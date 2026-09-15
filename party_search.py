# -*- coding: utf-8 -*-
"""
See You Travels — бот-разведчик вечеринок и событий.

Ищет, где в ближайшее время проходят вечеринки, концерты, DJ-сеты и другие
тусовки в выбранном городе (по умолчанию — Каш, Турция), и публикует дайджест
в Telegram-канал или чат.

Откуда берёт информацию (всё бесплатно, без ключей):
  1. Публичные Telegram-каналы из party_sources.TELEGRAM_CHANNELS —
     читает веб-превью t.me/s/<канал>, отбирает свежие посты по ключевым словам.
  2. Поиск DuckDuckGo по запросам из party_sources.SEARCH_QUERIES.

Необязательно: если задан ANTHROPIC_API_KEY, бот дополнительно просит Claude
поискать события в интернете и вернуть аккуратный список (это платно, по тарифу API).

Что уже показывали, запоминается в party_seen.txt — в новый дайджест попадают
только новые находки. Если нового ничего нет, бот молчит.

Секреты: TELEGRAM_TOKEN (обязательно), TELEGRAM_CHANNEL или PARTY_CHAT (куда слать),
ANTHROPIC_API_KEY (по желанию). Настройки города и слов — в party_sources.py
или через переменные PARTY_CITY / PARTY_CITY_EN.

Запуск локально для проверки, без отправки в Telegram:
    python party_search.py --dry-run
"""

import argparse
import datetime
import html
import json
import os
import re
import sys
from urllib.parse import parse_qs, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup

import party_sources as cfg

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT = (os.environ.get("PARTY_CHAT") or os.environ.get("TELEGRAM_CHANNEL") or "").strip()
CITY = os.environ.get("PARTY_CITY", cfg.CITY_RU).strip() or cfg.CITY_RU
CITY_EN = os.environ.get("PARTY_CITY_EN", cfg.CITY_EN).strip() or cfg.CITY_EN
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

SEEN_FILE = "party_seen.txt"
SEEN_LIMIT = 600            # сколько ссылок помнить (старые вычищаются)
MAX_ITEMS = 12              # максимум находок в одном дайджесте
TG_LIMIT = 4000             # лимит Telegram — 4096 символов, оставляем запас
TIMEOUT = 30
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CITY_WORDS = {w.lower() for w in [CITY, CITY_EN] + list(cfg.NEARBY)}
# Латинское написание без турецких букв (Kaş → kas), чтобы ловить и такие упоминания.
CITY_WORDS.add(CITY_EN.lower().replace("ş", "s").replace("ı", "i").replace("ç", "c").replace("ü", "u").replace("ö", "o").replace("ğ", "g"))


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Фильтры: похоже ли на вечеринку и относится ли к нашему городу
# ---------------------------------------------------------------------------
def looks_like_party(text):
    t = (text or "").lower()
    if not t:
        return False
    if any(s in t for s in cfg.STOP_WORDS):
        return False
    return any(k in t for k in cfg.PARTY_KEYWORDS)


def mentions_city(text):
    t = (text or "").lower()
    return any(w in t for w in CITY_WORDS)


def clean_url(url):
    """Убирает utm-хвосты и лишнее, чтобы одна и та же ссылка не считалась новой."""
    if not url:
        return ""
    p = urlparse(url.strip())
    if not p.scheme:
        p = urlparse("https://" + url.strip())
    query = "&".join(
        kv for kv in p.query.split("&")
        if kv and not kv.lower().startswith(("utm_", "fbclid", "igsh", "ref="))
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme, p.netloc.lower(), path, "", query, ""))


def shorten(text, limit=320):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut + "…"


# ---------------------------------------------------------------------------
# Источник 1: публичные Telegram-каналы (веб-превью t.me/s/<канал>)
# ---------------------------------------------------------------------------
def fetch_telegram_channel(username, since):
    url = f"https://t.me/s/{username.lstrip('@')}"
    items = []
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    except Exception as e:
        log(f"  t.me/{username}: ошибка сети — {e}")
        return items
    if r.status_code != 200:
        log(f"  t.me/{username}: HTTP {r.status_code}")
        return items

    soup = BeautifulSoup(r.text, "html.parser")
    for msg in soup.select(".tgme_widget_message"):
        text_el = msg.select_one(".tgme_widget_message_text")
        time_el = msg.select_one("time[datetime]")
        link_el = msg.select_one("a.tgme_widget_message_date")
        if not text_el:
            continue
        text = text_el.get_text("\n", strip=True)
        when = None
        if time_el and time_el.get("datetime"):
            try:
                when = datetime.datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
            except ValueError:
                when = None
        if when and when < since:
            continue
        if not looks_like_party(text):
            continue
        post_url = link_el["href"] if link_el and link_el.get("href") else url
        items.append({
            "source": f"Telegram @{username}",
            "title": shorten(text.split("\n")[0], 90),
            "url": post_url,
            "when": when.astimezone(datetime.timezone(datetime.timedelta(hours=3))).strftime("%d.%m") if when else "",
            "place": "",
            "summary": shorten(text),
        })
    log(f"  t.me/{username}: подходящих постов — {len(items)}")
    return items


# ---------------------------------------------------------------------------
# Источник 2: DuckDuckGo (HTML-версия, без ключей)
# ---------------------------------------------------------------------------
def _ddg_unwrap(href):
    """DuckDuckGo оборачивает ссылки в редирект //duckduckgo.com/l/?uddg=<url>."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    if "duckduckgo.com" in p.netloc and p.path.startswith("/l/"):
        target = parse_qs(p.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def search_duckduckgo(query):
    items = []
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "tr-tr"},
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.8,tr;q=0.6"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        log(f"  DDG «{query}»: ошибка сети — {e}")
        return items
    if r.status_code != 200:
        log(f"  DDG «{query}»: HTTP {r.status_code}")
        return items

    soup = BeautifulSoup(r.text, "html.parser")
    for res in soup.select(".result"):
        a = res.select_one("a.result__a")
        if not a:
            continue
        link = _ddg_unwrap(a.get("href", ""))
        title = a.get_text(" ", strip=True)
        snippet_el = res.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        blob = f"{title} {snippet}"
        if not link or "duckduckgo.com" in link:
            continue
        if not (looks_like_party(blob) and mentions_city(blob)):
            continue
        items.append({
            "source": "Поиск",
            "title": shorten(title, 90),
            "url": link,
            "when": "",
            "place": "",
            "summary": shorten(snippet),
        })
    log(f"  DDG «{query}»: подходящих результатов — {len(items)}")
    return items


# ---------------------------------------------------------------------------
# Источник 3 (по желанию): Claude с веб-поиском. Нужен ANTHROPIC_API_KEY.
# ---------------------------------------------------------------------------
def ai_search(seen_urls):
    try:
        import anthropic
    except ImportError:
        log("  ИИ-поиск: библиотека anthropic не установлена — пропускаем.")
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today = datetime.date.today().isoformat()
    already = "\n".join(list(seen_urls)[-80:])
    prompt = f"""Сегодня {today}. Найди в интернете вечеринки, DJ-сеты, концерты, beach-party, фестивали,
открытия/закрытия сезона и похожие тусовки, которые пройдут в ближайшие 14 дней в городе {CITY_EN}
({CITY}, Турция) и в радиусе примерно 30 км (Калкан, Демре и т.п.).
Ищи по-русски, по-английски и по-турецки: сайты афиш, Instagram и Telegram заведений
(бары, beach-клубы, марины, отели), локальные новости.

Верни ТОЛЬКО JSON-массив без пояснений. Каждый элемент:
{{"title": "название", "when": "дата и время, если известны", "place": "заведение/адрес",
  "url": "ссылка на источник", "summary": "1–2 предложения по-русски: что за событие и почему стоит пойти"}}

Правила: только реальные события с подтверждением по ссылке; не выдумывай; если точной даты нет,
так и напиши в when. Не включай события по этим ссылкам (они уже публиковались):
{already or "(пока ничего)"}
Если ничего не нашлось — верни []."""

    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 10,
        "user_location": {"type": "approximate", "city": CITY_EN,
                          "country": cfg.COUNTRY_CODE, "timezone": cfg.TIMEZONE},
    }]
    messages = [{"role": "user", "content": prompt}]
    response = None
    for _ in range(4):   # pause_turn: сервер мог прервать длинный поиск — продолжаем
        with client.messages.stream(
            model="claude-opus-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            tools=tools,
            messages=messages,
        ) as stream:
            response = stream.get_final_message()
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})

    if response is None or response.stop_reason == "refusal":
        log("  ИИ-поиск: ответа нет.")
        return []
    text = "".join(b.text for b in response.content if b.type == "text")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        log(f"  ИИ-поиск: не нашёл JSON в ответе: {text[:200]!r}")
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        log(f"  ИИ-поиск: JSON не разобрать — {e}")
        return []

    items = []
    for ev in data if isinstance(data, list) else []:
        if not isinstance(ev, dict) or not ev.get("title"):
            continue
        items.append({
            "source": "ИИ-поиск",
            "title": shorten(str(ev.get("title", "")), 90),
            "url": str(ev.get("url", "")).strip(),
            "when": shorten(str(ev.get("when", "")), 60),
            "place": shorten(str(ev.get("place", "")), 80),
            "summary": shorten(str(ev.get("summary", ""))),
        })
    log(f"  ИИ-поиск: событий — {len(items)}")
    return items


# ---------------------------------------------------------------------------
# Память: что уже публиковали
# ---------------------------------------------------------------------------
def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except FileNotFoundError:
        return []


def save_seen(urls):
    urls = urls[-SEEN_LIMIT:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        f.write("# Ссылки, которые уже были в дайджестах вечеринок. Файл ведёт бот.\n")
        f.write("\n".join(urls) + ("\n" if urls else ""))


# ---------------------------------------------------------------------------
# Сборка дайджеста и отправка
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(s or "", quote=False)


def format_item(i, it):
    lines = [f"<b>{i}. {esc(it['title'])}</b>"]
    meta = " · ".join(x for x in [it.get("when"), it.get("place")] if x)
    if meta:
        lines.append(f"<i>{esc(meta)}</i>")
    if it.get("summary") and it["summary"] != it["title"]:
        lines.append(esc(it["summary"]))
    if it.get("url"):
        lines.append(f'🔗 <a href="{html.escape(it["url"], quote=True)}">{esc(it["source"])}</a>')
    else:
        lines.append(f"📌 {esc(it['source'])}")
    return "\n".join(lines)


def build_digest(items, now):
    city_tag = "#" + re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", CITY)
    header = (f"🎉 <b>Где тусоваться: {esc(CITY)}</b>\n"
              f"<i>Свежие вечеринки и события · {now:%d.%m.%Y}</i>\n")
    footer = f"\n#вечеринки {city_tag} #SeeYouTravels"

    chunks, current = [], header
    for i, it in enumerate(items, 1):
        block = "\n" + format_item(i, it) + "\n"
        if len(current) + len(block) + len(footer) > TG_LIMIT:
            chunks.append(current.rstrip() + "\n" + footer)
            current = "<i>…продолжение</i>\n"
        current += block
    chunks.append(current.rstrip() + "\n" + footer)
    return chunks


def send_to_telegram(text_html):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT,
        "text": text_html,
        "parse_mode": "HTML",
        "link_preview_options": json.dumps({"is_disabled": True}),
    }
    r = requests.post(api, data=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:500]}")


# ---------------------------------------------------------------------------
def collect(seen_set, seen_list):
    found = []
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=cfg.LOOKBACK_DAYS)

    if ANTHROPIC_API_KEY:
        log("ИИ-поиск через Claude (ANTHROPIC_API_KEY задан):")
        try:
            found += ai_search(seen_list)
        except Exception as e:
            log(f"  ИИ-поиск: ошибка — {e}")
    else:
        log("ИИ-поиск выключен (нет ANTHROPIC_API_KEY) — ищем бесплатными способами.")

    if cfg.TELEGRAM_CHANNELS:
        log("Telegram-каналы:")
        for ch in cfg.TELEGRAM_CHANNELS:
            found += fetch_telegram_channel(ch, since)
    else:
        log("Telegram-каналы не заданы (party_sources.TELEGRAM_CHANNELS пуст).")

    log("DuckDuckGo:")
    for q in cfg.SEARCH_QUERIES:
        found += search_duckduckgo(q.format(city=CITY, city_en=CITY_EN))

    # Убираем дубли и то, что уже публиковали. Порядок: ИИ → Telegram → поиск.
    fresh, batch_keys = [], set()
    for it in found:
        key = clean_url(it.get("url")) or ("t:" + it["title"].lower())
        if key in seen_set or key in batch_keys:
            continue
        batch_keys.add(key)
        it["key"] = key
        fresh.append(it)
    return fresh


def main(argv=None):
    ap = argparse.ArgumentParser(description="Поиск вечеринок и отправка дайджеста в Telegram.")
    ap.add_argument("--dry-run", action="store_true", help="только показать, ничего не отправлять")
    ap.add_argument("--all", action="store_true", help="не учитывать party_seen.txt (показать всё найденное)")
    args = ap.parse_args(argv)

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))   # МСК
    log(f"{now:%Y-%m-%d %H:%M} МСК — ищем вечеринки: {CITY} / {CITY_EN}")

    seen_list = [] if args.all else load_seen()
    fresh = collect(set(seen_list), seen_list)
    log(f"Новых находок: {len(fresh)}")

    if not fresh:
        log("Ничего нового — дайджест не отправляем.")
        return

    fresh = fresh[:MAX_ITEMS]
    chunks = build_digest(fresh, now)

    if args.dry_run:
        for c in chunks:
            log("-" * 60)
            log(c)
        log("-" * 60)
        log("(--dry-run: в Telegram не отправлено, party_seen.txt не изменён)")
        return

    if not TELEGRAM_TOKEN or not CHAT:
        raise RuntimeError("Нужны TELEGRAM_TOKEN и TELEGRAM_CHANNEL (или PARTY_CHAT).")
    for c in chunks:
        send_to_telegram(c)
    log(f"  Опубликовано в {CHAT} ✅ (сообщений: {len(chunks)})")

    save_seen(load_seen() + [it["key"] for it in fresh])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        sys.exit(1)

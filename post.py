# -*- coding: utf-8 -*-
"""
See You Travels — ежедневный бот-постер для Telegram-канала.
Версия без ИИ и без оплаты: берёт готовые посты из content_bank.py.

Логика:
  - По понедельникам (по Москве) публикует АНОНС путешествия (пул ANNOUNCEMENTS).
  - В остальные дни — обычную рубрику (пул POSTS).
  - Каждый пост выходит с подписью рубрики сверху и хэштегами снизу.
  - Фото подбирается автоматически: Pexels → Openverse.
  - Что уже выходило, помечается в used_posts.txt (посты — p0,p1…; анонсы — a0,a1…).
    Когда пул исчерпан — круг начинается заново.

Секреты (в GitHub — это Secrets): TELEGRAM_TOKEN, TELEGRAM_CHANNEL, PEXELS_KEY.
"""

import os
import sys
import json
import re
import datetime
from zoneinfo import ZoneInfo

import requests

from content_bank import POSTS, ANNOUNCEMENTS

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]           # например @see_you_travels
PEXELS_KEY = os.environ.get("PEXELS_KEY", "").strip()

TZ = ZoneInfo("Europe/Moscow")
ANNOUNCEMENT_WEEKDAY = 0        # 0 = понедельник — день анонса
USED_FILE = "used_posts.txt"
TIMEOUT = 40

# ---------------------------------------------------------------------------
# Оформление рубрик: подпись сверху + хэштеги снизу (навигация по темам).
# ---------------------------------------------------------------------------
RUBRIC_META = {
    "Направление недели":       {"emoji": "🗺", "name": "Направление недели", "tag": "#направление_недели", "place": True},
    "Скрытые места":            {"emoji": "📍", "name": "Скрытые места",       "tag": "#скрытые_места",      "place": True},
    "Вау-новость мира":         {"emoji": "🌍", "name": "Вау-новости мира",    "tag": "#вау_новости",       "place": True},
    "Лайфхак / совет":          {"emoji": "💡", "name": "Лайфхаки",            "tag": "#лайфхаки",          "place": False},
    "Вдохновение / вовлечение": {"emoji": "✨", "name": "Вдохновение",         "tag": "#вдохновение",       "place": False},
    "Анонс путешествия":        {"emoji": "🌟", "name": "Авторские путешествия", "tag": "#путешествия_с_нами", "place": False},
}
BRAND_TAG = "#SeeYouTravels"


def log(msg):
    print(msg, flush=True)


def build_message(item):
    """Собирает финальный текст: подпись рубрики + сам пост + хэштеги."""
    post = item["post"].strip()
    rubric = item.get("rubric", "")
    topic = item.get("topic", "").strip()
    meta = RUBRIC_META.get(rubric)

    tags = []
    header = None
    if meta:
        header = f'{meta["emoji"]} <i>Рубрика «{meta["name"]}»</i>'
        tags.append(meta["tag"])
        if meta["place"] and topic:
            topic_tag = "#" + re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", topic)
            if len(topic_tag) > 2:
                tags.append(topic_tag)
    tags.append(BRAND_TAG)

    body = f"{header}\n\n{post}" if header else post
    return f"{body}\n\n{' '.join(tags)}"


# ---------------------------------------------------------------------------
# Выбор поста по кругу. prefix: "p" — обычные посты, "a" — анонсы.
# ---------------------------------------------------------------------------
def _read_used():
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _write_used(lines):
    with open(USED_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def pick(pool, prefix):
    used = _read_used()
    keys = set(used)
    n = len(pool)
    # Весь пул использован — сбрасываем только его отметки (prefix + цифры).
    if all(f"{prefix}{i}" in keys for i in range(n)):
        log(f"  Пул '{prefix}' пройден полностью — начинаем круг заново.")
        used = [u for u in used if not re.fullmatch(prefix + r"\d+", u)]
        _write_used(used)
        keys = set(used)
    for i in range(n):
        if f"{prefix}{i}" not in keys:
            return i, pool[i]
    return 0, pool[0]


def mark_used(prefix, index):
    with open(USED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{prefix}{index}\n")


# Защита от дублей: отметка дня публикации хранится в том же used_posts.txt
# в виде строки "d:2026-08-01". Так ничего в расписании менять не нужно.
def already_posted_today(today):
    return f"d:{today}" in set(_read_used())


# ---------------------------------------------------------------------------
# Поиск фотографии: Pexels → Openverse (без ключа)
# ---------------------------------------------------------------------------
def get_image(query):
    if PEXELS_KEY:
        try:
            r = requests.get(
                "https://api.pexels.com/v1/search",
                headers={"Authorization": PEXELS_KEY},
                params={"query": query, "per_page": 10, "orientation": "landscape"},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                photos = r.json().get("photos", [])
                if photos:
                    src = photos[0]["src"]
                    return src.get("large2x") or src.get("large") or src.get("original")
            else:
                log(f"  Pexels {r.status_code}: {r.text[:200]}")
        except Exception as e:
            log(f"  Pexels ошибка: {e}")

    try:
        r = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "page_size": 5, "license_type": "commercial"},
            headers={"User-Agent": "SeeYouTravelsBot/1.0"},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                return results[0].get("url")
    except Exception as e:
        log(f"  Openverse ошибка: {e}")

    return None


# ---------------------------------------------------------------------------
# Публикация в Telegram (фото сверху + длинный текст)
# ---------------------------------------------------------------------------
def send_to_telegram(text_html, image_url):
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL,
        "text": text_html,
        "parse_mode": "HTML",
    }
    if image_url:
        payload["link_preview_options"] = json.dumps({
            "url": image_url,
            "prefer_large_media": True,
            "show_above_text": True,
        })
    else:
        payload["link_preview_options"] = json.dumps({"is_disabled": True})

    r = requests.post(api, data=payload, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram {r.status_code}: {r.text[:500]}")
    log("  Опубликовано в канал ✅")
    return r.json()


def main():
    now = datetime.datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")

    # Защита от дублей: если сегодня уже публиковали — выходим без поста.
    if already_posted_today(today):
        log(f"{today}: сегодня уже публиковали — пропускаем (защита от дублей).")
        return

    is_announcement_day = now.weekday() == ANNOUNCEMENT_WEEKDAY and bool(ANNOUNCEMENTS)

    if is_announcement_day:
        prefix, pool = "a", ANNOUNCEMENTS
        log(f"{now:%Y-%m-%d %H:%M} МСК — ПОНЕДЕЛЬНИК: публикуем анонс путешествия.")
    else:
        prefix, pool = "p", POSTS
        log(f"{now:%Y-%m-%d %H:%M} МСК — обычная рубрика.")

    index, item = pick(pool, prefix)
    query = item.get("image_query", item.get("topic", ""))
    log(f"  Пост {prefix}{index} | Рубрика: {item.get('rubric','?')} | Тема: {item.get('topic','')}")
    log(f"  Фото по запросу: {query}")

    image_url = get_image(query)
    log(f"  Фото: {image_url or 'не найдено — публикуем без картинки'}")

    text = build_message(item)
    send_to_telegram(text, image_url)
    mark_used(prefix, index)
    mark_used("d:", today)   # отмечаем, что сегодня пост уже вышел


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        sys.exit(1)

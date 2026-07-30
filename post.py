# -*- coding: utf-8 -*-
"""
See You Travels — ежедневный бот-постер для Telegram-канала.
Версия без ИИ и без оплаты: берёт готовые посты из content_bank.py.

Что делает при запуске:
  1. Берёт следующий неиспользованный пост из банка (content_bank.py)
  2. Ищет под него красивое реальное фото: сначала Pexels, если нет — Openverse
  3. Публикует пост с фото в канал через Telegram-бота
  4. Запоминает, какой пост уже вышел (used_posts.txt). Когда все использованы — круг начинается заново.

Секреты берутся из переменных окружения (в GitHub — это Secrets):
  TELEGRAM_TOKEN, TELEGRAM_CHANNEL, PEXELS_KEY (необязательно)
"""

import os
import sys
import json
import re

import requests

from content_bank import POSTS

# ---------------------------------------------------------------------------
# Оформление рубрик: подпись сверху + хэштеги снизу (для навигации по темам).
# Меняется здесь — применяется сразу ко всем постам, текущим и будущим.
# ---------------------------------------------------------------------------
RUBRIC_META = {
    "Направление недели":       {"emoji": "🗺", "name": "Направление недели", "tag": "#направление_недели", "place": True},
    "Скрытые места":            {"emoji": "📍", "name": "Скрытые места",       "tag": "#скрытые_места",      "place": True},
    "Вау-новость мира":         {"emoji": "🌍", "name": "Вау-новости мира",    "tag": "#вау_новости",       "place": True},
    "Лайфхак / совет":          {"emoji": "💡", "name": "Лайфхаки",            "tag": "#лайфхаки",          "place": False},
    "Вдохновение / вовлечение": {"emoji": "✨", "name": "Вдохновение",         "tag": "#вдохновение",       "place": False},
}
BRAND_TAG = "#SeeYouTravels"


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

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL = os.environ["TELEGRAM_CHANNEL"]           # например @see_you_travels
PEXELS_KEY = os.environ.get("PEXELS_KEY", "").strip()

USED_FILE = "used_posts.txt"
TIMEOUT = 40


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Выбор следующего поста (по кругу, без повторов до конца круга)
# ---------------------------------------------------------------------------
def read_used():
    try:
        with open(USED_FILE, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def pick_post():
    used = read_used()
    # Все посты уже выходили — начинаем новый круг.
    if len(used) >= len(POSTS):
        log("  Все посты из банка использованы — начинаем круг заново.")
        used = set()
        open(USED_FILE, "w").close()
    for i, item in enumerate(POSTS):
        key = str(i)
        if key not in used:
            return i, item
    # На всякий случай (не должно случиться)
    return 0, POSTS[0]


def mark_used(index):
    with open(USED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{index}\n")


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
    index, item = pick_post()
    topic = item.get("topic", "пост")
    query = item.get("image_query", topic)
    post = item["post"].strip()
    log(f"Пост #{index} | Рубрика: {item.get('rubric','?')} | Тема: {topic}")
    log(f"  Фото по запросу: {query}")

    image_url = get_image(query)
    log(f"  Фото: {image_url or 'не найдено — публикуем без картинки'}")

    text = build_message(item)
    send_to_telegram(text, image_url)
    mark_used(index)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ОШИБКА: {e}")
        sys.exit(1)

"""
Бот для Telegram-каналу: стежить за графіком відключень Львівобленерго
і публікує його в канал, коли графік змінюється.

Дані беруться напряму з API, яким користується сайт poweron.loe.lviv.ua:
там лежить офіційна картинка графіка і текст по групах, окремо
для «сьогодні» і «завтра». Браузер не потрібен.

Змінні середовища (у GitHub — Secrets):
  BOT_TOKEN    — токен бота від @BotFather (обов'язково)
  CHANNEL_ID   — @назва_каналу або -100... (обов'язково)
  LOOP_MINUTES — для Мака/VPS: перевіряти безперервно раз на N хв
  DRY_RUN=1    — нічого не надсилати, лише показати, що було б опубліковано
"""

import hashlib
import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

API_URL = "https://api.loe.lviv.ua/api/menus?page=1&type=photo-grafic&jsonld=1"
MEDIA_BASE = "https://api.loe.lviv.ua"

DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0") or 0)

STATE_FILE = Path("state/posted.json")
DAYS = {"Today": "Сьогодні", "Tomorrow": "Завтра"}

HEADERS = {"User-Agent": "Mozilla/5.0 (schedule-bot)"}


def fetch_items():
    """Повертає пункти «Today»/«Tomorrow», у яких є картинка графіка."""
    r = requests.get(API_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = []
    for menu in r.json().get("hydra:member", []):
        for it in menu.get("menuItems", []):
            if it.get("name") in DAYS and it.get("imageUrl"):
                items.append(it)
    return items


def html_lines(raw_html: str):
    paras = re.findall(r"<p[^>]*>(.*?)</p>", raw_html or "", flags=re.S)
    return [unescape(re.sub(r"<[^>]+>", "", p)).strip() for p in paras]


def make_caption(item) -> str:
    text = "\n".join(html_lines(item.get("rawHtml", "")))
    day = DAYS[item["name"]]

    m = re.search(r"на\s+(\d{2}\.\d{2}\.\d{4})", text)
    date = m.group(1) if m else ""
    m = re.search(r"станом на\s+(\d{1,2}:\d{2})\s+(\d{2}\.\d{2}\.\d{4})", text)
    upd_time, upd_date = (m.group(1), m.group(2)) if m else ("", "")

    title = f"💡 Оновлений Графік на {day}"
    if date:
        title += f" ({date})"
    lines = [title]
    if upd_time:
        lines.append(f"Останнє оновлення - {upd_time}")
        lines.append(upd_date)
    return "\n".join(lines)


def fingerprint(item) -> str:
    # Рахуємо по тексту графіка (у ньому є дата і час «станом на»), а не по назві
    # пункту: опівночі «завтрашній» графік стає «сьогоднішнім», і повторно
    # публікувати його не треба. Якщо тексту немає — беремо адресу картинки.
    basis = item.get("rawHtml") or item.get("imageUrl", "")
    basis = re.sub(r"\s+", " ", basis).strip()
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {"posted": []}


def save_state(state):
    state["posted"] = state["posted"][-50:]
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def send_photo(image: bytes, caption: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data={"chat_id": CHANNEL_ID, "caption": caption},
        files={"photo": ("grafik.png", image, "image/png")},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def main():
    try:
        items = fetch_items()
    except requests.RequestException as e:
        # Сайт LOE тимчасово недоступний: не падаємо, спробуємо наступного разу.
        print(f"LOE API недоступний: {e}")
        return

    state = load_state()
    # Спершу «сьогодні», потім «завтра», щоб у каналі був логічний порядок.
    items.sort(key=lambda it: list(DAYS).index(it["name"]))

    for item in items:
        fp = fingerprint(item)
        if fp in state["posted"]:
            print(f"{item['name']}: без змін.")
            continue

        caption = make_caption(item)
        img_url = MEDIA_BASE + item["imageUrl"]
        print(f"{item['name']}: новий графік → публікую\n{caption}\n{img_url}\n")

        if not DRY_RUN:
            img = requests.get(img_url, headers=HEADERS, timeout=60)
            img.raise_for_status()
            send_photo(img.content, caption)
            state["posted"].append(fp)
            save_state(state)


if __name__ == "__main__":
    if not DRY_RUN and not (BOT_TOKEN and CHANNEL_ID):
        sys.exit("Задай BOT_TOKEN і CHANNEL_ID")
    if not LOOP_MINUTES:
        main()
    else:
        while True:
            try:
                main()
            except Exception as e:
                print(f"Помилка: {e}")
            time.sleep(LOOP_MINUTES * 60)

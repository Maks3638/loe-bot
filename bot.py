"""
Бот для Telegram-каналу: стежить за графіком відключень Львівобленерго
і публікує його в канал, коли з'являється нова версія.

Джерело: API, яким користується сайт poweron.loe.lviv.ua (офіційна картинка
графіка + текст по групах, окремо на сьогодні і на завтра).

Як бот вирішує, що публікувати (щоб не було дублів):
  кожна версія графіка — це пара (дата графіка, час «Інформація станом на»).
  Для кожної дати бот пам'ятає час останньої опублікованої версії і публікує
  лише СТРОГО НОВІШУ. Ту саму або старішу версію він пропускає, хоч би звідки
  вона прийшла.

Змінні середовища (у GitHub — Secrets):
  BOT_TOKEN    — токен бота від @BotFather (обов'язково)
  CHANNEL_ID   — @назва_каналу або -100... (обов'язково)
  LOOP_MINUTES — перевіряти безперервно раз на N хв (Мак/VPS/GitHub-цикл)
  LOOP_TOTAL_MINUTES — скільки всього крутити цикл (0 = безкінечно)
  DRY_RUN=1    — нічого не надсилати, лише показати, що було б опубліковано
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://api.loe.lviv.ua/api/menus"
MEDIA_BASE = "https://api.loe.lviv.ua"
KYIV = ZoneInfo("Europe/Kyiv")

DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0") or 0)
LOOP_TOTAL_MINUTES = float(os.environ.get("LOOP_TOTAL_MINUTES", "0") or 0)

STATE_FILE = Path("state/state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (schedule-bot)", "Accept": "application/ld+json"}


# ---------- версія графіка ----------

class Version:
    """Одна версія графіка з будь-якого джерела."""

    def __init__(self, date: str, upd: datetime, image_url: str, source: str):
        self.date = date          # "21.11.2026" — на який день графік
        self.upd = upd            # коли LOE його видало («станом на»)
        self.image_url = image_url
        self.source = source

    @property
    def key(self):
        return self.upd.strftime("%Y-%m-%d %H:%M")

    def __repr__(self):
        return f"<{self.source}: графік на {self.date}, станом на {self.key}>"


def parse_upd(text: str):
    """«станом на 23:35 20.11.2026» → datetime або None."""
    m = re.search(r"станом на\s*(\d{1,2}):(\d{2})\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    if not m:
        return None
    hh, mm, d, mo, y = map(int, m.groups())
    try:
        return datetime(y, mo, d, hh, mm)
    except ValueError:
        return None


def valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%d.%m.%Y")
        return True
    except ValueError:
        return False


# ---------- джерело: сайт ----------

def from_site():
    # Параметр _ обходить кеш Cloudflare (інакше відповідь може бути до 2 год старою).
    params = {"page": 1, "type": "photo-grafic", "jsonld": 1, "_": int(time.time())}
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    menus = data.get("hydra:member", []) if isinstance(data, dict) else data

    versions = []
    for menu in menus:
        for it in menu.get("menuItems", []):
            if it.get("name") not in ("Today", "Tomorrow") or not it.get("imageUrl"):
                continue
            text = unescape(re.sub(r"<[^>]+>", " ", it.get("rawHtml") or ""))
            m = re.search(r"відключень на\s*(\d{2}\.\d{2}\.\d{4})", text)
            upd = parse_upd(text)
            if not (m and valid_date(m.group(1)) and upd):
                print(f"Сайт: не вдалося прочитати дату/час у пункті {it.get('name')}, пропускаю")
                continue
            versions.append(Version(m.group(1), upd, MEDIA_BASE + it["imageUrl"], "сайт"))
    return versions


# ---------- стан ----------

def load_state():
    try:
        st = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        st = {}
    st.setdefault("dates", {})
    return st


def save_state(st):
    # Тримаємо лише останні 14 днів.
    keep = sorted(st["dates"], key=lambda d: datetime.strptime(d, "%d.%m.%Y"))[-14:]
    st["dates"] = {d: st["dates"][d] for d in keep}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    if os.environ.get("PUSH_STATE"):
        push_state()


def push_state():
    """На GitHub: одразу комітимо пам'ять бота, щоб наступний запуск її бачив."""
    git = lambda *a: subprocess.run(["git", *a], check=True)
    git("add", str(STATE_FILE))
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    git("-c", "user.name=schedule-bot", "-c", "user.email=bot@users.noreply.github.com",
        "commit", "-q", "-m", "Опубліковано новий графік")
    for attempt in range(4):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("Не вдалося зберегти стан у репозиторій")


# ---------- публікація ----------

def make_caption(v: Version) -> str:
    today = datetime.now(KYIV).date()
    d = datetime.strptime(v.date, "%d.%m.%Y").date()
    day = {today: "Сьогодні", today + timedelta(days=1): "Завтра"}.get(d)
    title = f"💡 Оновлений Графік на {day} ({v.date})" if day else f"💡 Оновлений Графік на {v.date}"
    return f"{title}\nОстаннє оновлення - {v.upd:%H:%M}\n{v.upd:%d.%m.%Y}"


def send_photo(image: bytes, caption: str):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
        data={"chat_id": CHANNEL_ID, "caption": caption},
        files={"photo": ("grafik.png", image, "image/png")},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


def check_once():
    versions = []
    for name, fn in (("сайт", from_site),):
        try:
            versions += fn()
        except Exception as e:  # одне джерело лягло — працюємо з іншими
            print(f"Джерело «{name}» недоступне: {e}")

    st = load_state()
    today = datetime.now(KYIV).date()

    # Найсвіжіша версія для кожної дати (якщо джерела дали кілька).
    best = {}
    for v in versions:
        if datetime.strptime(v.date, "%d.%m.%Y").date() < today:
            continue  # графік на минулий день не публікуємо
        if v.date not in best or v.upd > best[v.date].upd:
            best[v.date] = v

    for date in sorted(best, key=lambda d: datetime.strptime(d, "%d.%m.%Y")):
        v = best[date]
        last = st["dates"].get(date, {}).get("upd", "")
        if last and v.key <= last:
            print(f"{v} — вже опубліковано (останнє: {last}), пропускаю")
            continue

        caption = make_caption(v)
        print(f"{v} — НОВА версія, публікую:\n{caption}\n{v.image_url}\n")
        if DRY_RUN:
            continue
        img = requests.get(v.image_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
        img.raise_for_status()
        send_photo(img.content, caption)
        st["dates"][date] = {"upd": v.key, "source": v.source}
        save_state(st)  # зберігаємо одразу після кожного поста


def main():
    if not DRY_RUN and not (BOT_TOKEN and CHANNEL_ID):
        sys.exit("Задай BOT_TOKEN і CHANNEL_ID")
    if not LOOP_MINUTES:
        check_once()
        return
    started = time.time()
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"Помилка: {e}")
        if LOOP_TOTAL_MINUTES and time.time() - started + LOOP_MINUTES * 60 > LOOP_TOTAL_MINUTES * 60:
            break
        time.sleep(LOOP_MINUTES * 60)


if __name__ == "__main__":
    main()

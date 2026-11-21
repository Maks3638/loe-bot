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


# ---------- джерело: Telegram-канал LOE ----------
#
# Пости з графіком у каналі @lvivoblenergo — це картинки без тексту.
# Дату і час «станом на» розпізнаємо з картинки (Tesseract) і приймаємо
# пост лише тоді, коли ВСІ перевірки зійшлися:
#   1) заголовок картинки — «Графік погодинних відключень»;
#   2) дата в помаранчевій плашці — реальна дата;
#   3) рядок «станом на ЧЧ:ХХ ДД.ММ.РРРР» розпізнано повністю;
#   4) графік на той самий або наступний день після «станом на»;
#   5) час посту в Telegram відрізняється від «станом на» не більше ніж на 90 хв.
# Будь-який сумнів — пост пропускаємо (графік однаково прийде з сайту).

TG_CHANNEL = "lvivoblenergo"
TG_MAX_AGE_HOURS = 24


def _tess(img, args):
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f.name)
    try:
        return subprocess.run(["tesseract", f.name, "-", *args],
                              capture_output=True, text=True, timeout=60).stdout.strip()
    finally:
        os.unlink(f.name)


def _orange_box(im):
    """Координати помаранчевої плашки з датою у верхній частині картинки."""
    w, h = im.size
    px = im.load()
    pts = [(x, y) for y in range(int(h * 0.15)) for x in range(w)
           if px[x, y][0] > 200 and 120 < px[x, y][1] < 200 and px[x, y][2] < 90]
    if len(pts) < 200:
        return None
    ys = sorted({y for _, y in pts})
    y0 = y1 = ys[0]
    for y in ys[1:]:
        if y - y1 > 2:
            break
        y1 = y
    xs = [x for x, y in pts if y0 <= y <= y1]
    return (min(xs), y0, max(xs) + 1, y1 + 1)


def read_schedule_image(data: bytes):
    """Повертає (дата_графіка, datetime_станом_на) або None, якщо є сумнів."""
    from io import BytesIO
    from PIL import Image, ImageOps

    im = Image.open(BytesIO(data)).convert("RGB")
    w, h = im.size
    if not (1.5 < h / w < 3):  # графік у каналі — висока вертикальна картинка
        return None

    def big(c, k=4):
        return ImageOps.autocontrast(c.convert("L").resize((c.width * k, c.height * k), Image.LANCZOS))

    head = _tess(big(im.crop((0, 0, w, int(h * 0.045))), 3), ["-l", "ukr", "--psm", "7"]).lower()
    if "погодин" not in head and "відключ" not in head:
        return None

    box = _orange_box(im)
    if not box:
        return None
    date = _tess(big(im.crop(box)), ["--psm", "7", "-c", "tessedit_char_whitelist=0123456789."])
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", date) or not valid_date(date):
        return None

    bottom = _tess(big(im.crop((0, int(h * 0.95), int(w * 0.75), int(h * 0.985)))),
                   ["--psm", "7", "-c", "tessedit_char_whitelist=0123456789.:"])
    m = re.fullmatch(r"(\d{2}):(\d{2})\s*(\d{2})\.(\d{2})\.(\d{4})", bottom)
    if not m:
        return None
    upd = parse_upd("станом на {}:{} {}.{}.{}".format(*m.groups()))
    if not upd:
        return None

    sched = datetime.strptime(date, "%d.%m.%Y").date()
    if sched not in (upd.date(), upd.date() + timedelta(days=1)):
        return None
    return date, upd


def from_telegram():
    r = requests.get(f"https://t.me/s/{TG_CHANNEL}", headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    r.raise_for_status()
    page = r.text
    now = datetime.now(KYIV)

    versions = []
    chunks = re.split(r'(?=<div class="tgme_widget_message_wrap)', page)
    for chunk in chunks[-15:]:  # останні пости каналу
        post = re.search(r'data-post="([^"]+)"', chunk)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        photos = re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\('([^']+)'\)", chunk)
        if not (post and when and photos):
            continue
        posted = datetime.fromisoformat(when.group(1)).astimezone(KYIV)
        if now - posted > timedelta(hours=TG_MAX_AGE_HOURS):
            continue
        for url in photos:
            try:
                img = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
                img.raise_for_status()
                res = read_schedule_image(img.content)
            except Exception as e:
                print(f"Telegram {post.group(1)}: не вдалося обробити фото ({e})")
                continue
            if not res:
                continue  # не графік або розпізнано з сумнівом
            date, upd = res
            gap = abs(posted.replace(tzinfo=None) - upd)
            if gap > timedelta(minutes=90):
                print(f"Telegram {post.group(1)}: «станом на» {upd:%H:%M} не збігається з часом посту "
                      f"{posted:%H:%M}, пропускаю")
                continue
            v = Version(date, upd, url, "telegram")
            v.image_bytes = img.content
            versions.append(v)
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
        # У репозиторії з'явились нові коміти (наприклад, оновлення коду) — підтягуємо.
        subprocess.run(["git", "pull", "-q", "--rebase"])
        time.sleep(5 * (attempt + 1))
    raise StateNotSaved("Не вдалося зберегти стан у репозиторій")


class StateNotSaved(Exception):
    """Пам'ять не збереглась: далі не публікуємо, щоб не наробити дублів."""


# ---------- запобіжник: перевірка нашого власного каналу ----------
#
# Незалежно від файлу пам'яті бот перед публікацією перечитує свій канал
# (публічний перегляд t.me/s/...) і шукає там пости з підписом
# «(ДД.ММ.РРРР) ... Останнє оновлення - ЧЧ:ХХ ДД.ММ.РРРР». Якщо така або новіша
# версія для цієї дати вже є в каналі — не публікуємо.

_channel_username = None


def channel_username():
    global _channel_username
    if _channel_username is None:
        _channel_username = ""
        if CHANNEL_ID.startswith("@"):
            _channel_username = CHANNEL_ID[1:]
        elif BOT_TOKEN:
            try:
                r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChat",
                                 params={"chat_id": CHANNEL_ID}, timeout=30)
                _channel_username = (r.json().get("result") or {}).get("username") or ""
            except Exception as e:
                print(f"Не вдалося дізнатися юзернейм каналу: {e}")
                _channel_username = None  # спробуємо наступного разу
                return ""
        if _channel_username == "":
            print("Канал без публічного юзернейму — перевірку каналу вимкнено")
    return _channel_username or ""


def posted_in_channel():
    """{дата: найновіший "РРРР-ММ-ДД ЧЧ:ХХ"} з останніх постів нашого каналу, або None."""
    name = channel_username()
    if not name:
        return None
    r = requests.get(f"https://t.me/s/{name}", headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30)
    r.raise_for_status()
    text = unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", "\n", r.text)))
    text = re.sub(r"\s+", " ", text)
    found = {}
    for m in re.finditer(r"\((\d{2}\.\d{2}\.\d{4})\) Останнє оновлення - (\d{1,2}:\d{2}) (\d{2}\.\d{2}\.\d{4})", text):
        upd = parse_upd(f"станом на {m.group(2)} {m.group(3)}")
        if upd:
            key = upd.strftime("%Y-%m-%d %H:%M")
            found[m.group(1)] = max(found.get(m.group(1), ""), key)
    print(f"Перевірка каналу t.me/s/{name}: знайдено {found or 'нічого'}")
    return found


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
        files={"photo": ("grafik.jpg", image, "image/jpeg") if image[:2] == b"\xff\xd8"
               else ("grafik.png", image, "image/png")},
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")
    print(f"Опубліковано, message_id={r.json()['result']['message_id']}")


def check_once():
    versions = []
    for name, fn in (("сайт", from_site), ("telegram", from_telegram)):
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
        cur = best.get(v.date)
        # Новіша версія виграє; при однаковому часі — сайт (там чіткіша картинка).
        if cur is None or v.upd > cur.upd or (v.upd == cur.upd and v.source == "сайт"):
            best[v.date] = v

    channel = None
    for date in sorted(best, key=lambda d: datetime.strptime(d, "%d.%m.%Y")):
        v = best[date]
        last = st["dates"].get(date, {}).get("upd", "")
        if last and v.key <= last:
            print(f"{v} — вже опубліковано (останнє: {last}), пропускаю")
            continue

        # Другий запобіжник: дивимось, що реально є в нашому каналі.
        if channel is None:
            try:
                channel = posted_in_channel() or {}
            except Exception as e:
                print(f"Не вдалося перечитати канал ({e}), покладаюсь на пам'ять бота")
                channel = {}
        in_channel = channel.get(date, "")
        if in_channel and v.key <= in_channel:
            print(f"{v} — уже є в каналі (останнє там: {in_channel}), пропускаю і запам'ятовую")
            if not DRY_RUN:
                st["dates"][date] = {"upd": in_channel, "source": "канал"}
                save_state(st)
            continue

        caption = make_caption(v)
        print(f"{v} — НОВА версія, публікую:\n{caption}\n{v.image_url}\n")
        if DRY_RUN:
            continue
        data = getattr(v, "image_bytes", None)
        if data is None:
            img = requests.get(v.image_url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=60)
            img.raise_for_status()
            data = img.content
        send_photo(data, caption)
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
        except StateNotSaved as e:
            sys.exit(f"СТОП: {e}")
        except Exception as e:
            print(f"Помилка: {e}")
        if LOOP_TOTAL_MINUTES and time.time() - started + LOOP_MINUTES * 60 > LOOP_TOTAL_MINUTES * 60:
            break
        time.sleep(LOOP_MINUTES * 60)


if __name__ == "__main__":
    main()

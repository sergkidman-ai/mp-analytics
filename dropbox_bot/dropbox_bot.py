# -*- coding: utf-8 -*-
# поток: inv
"""
dropbox_bot.py — личный Telegram-«ящик» для файлов и скриншотов.

Кидаешь боту фото/скриншот/документ (или текст-заметку) → бот кладёт его в папку
на сервере (/opt/mp-analytics/dropbox/) и подтверждает приём. Дальше в рабочей
сессии Claude Code достаточно сказать «глянь dropbox» — файлы уже на диске.

Это ОТДЕЛЬНЫЙ бот (не боевой @MC_invoicebot): сюда шлём материал НА РАЗБОР, он
НИКУДА не уходит автоматически — только сохраняется. Своя автоуборка (7 дней,
см. invoice_bot/cleanup_inbox.py).

Зависимостей нет — long-polling на urllib. Токен и allow-list из /opt/mp-analytics/.env:
    DROPBOX_BOT_TOKEN=123456:AA...
    DROPBOX_ALLOWED_IDS=11111111,22222222     # numeric TG id, через запятую

Пока токена нет или ID не в списке — бот сообщает твой ID (безопасный bootstrap).
Запуск: python dropbox_bot.py   (в бою — под systemd, dropbox-bot.service)
"""
import os, sys, re, json, time, urllib.request, urllib.parse, urllib.error, traceback

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")

TOKEN = os.getenv("DROPBOX_BOT_TOKEN", "").strip()
ALLOWED = {x.strip() for x in os.getenv("DROPBOX_ALLOWED_IDS", "").split(",") if x.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
DROPBOX = "/opt/mp-analytics/dropbox"
INDEX = os.path.join(DROPBOX, "index.log")
os.makedirs(DROPBOX, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def api(method, params=None, timeout=60):
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def send(chat_id, text, reply_to=None):
    text = text if len(text) <= 4000 else text[:3990] + "\n…(обрезано)"
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    try:
        api("sendMessage", p)
    except Exception as e:
        log(f"sendMessage error: {e}")


def _sanitize(s, maxlen=80):
    """Оставляем буквы (лат/кир)/цифры/._- , остальное → _ ; схлопываем повторы."""
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "_", (s or "").strip())
    s = re.sub(r"_+", "_", s).strip("._")
    return (s[:maxlen] or "file")


def sender_label(msg):
    f = msg.get("from", {}) or {}
    name = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x).strip()
    if f.get("username"):
        name = (name + f" @{f['username']}").strip()
    return name or str(f.get("id", "?"))


def _uniq(path):
    """Не перетираем уже существующий файл — добавляем -2, -3…"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}-{i}{ext}"):
        i += 1
    return f"{stem}-{i}{ext}"


def download_to(file_id, dst_path):
    info = api("getFile", {"file_id": file_id})
    fp = info["result"]["file_path"]
    with urllib.request.urlopen(f"{FILE_API}/{fp}", timeout=180) as r:
        data = r.read()
    with open(dst_path, "wb") as f:
        f.write(data)
    return len(data), fp


def _human_size(n):
    if n < 1024:
        return f"{n} Б"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} КБ"
    return f"{n/1024**2:.1f} МБ"


def _index_line(name, note):
    try:
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{name}\t{note or ''}\n")
    except OSError:
        pass


def save_note(sender, ts, text):
    """Текстовое сообщение без файла → сохраняем как заметку .txt."""
    base = f"{ts}_{_sanitize(sender,40)}_note.txt"
    path = _uniq(os.path.join(DROPBOX, base))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _index_line(os.path.basename(path), "заметка")
    return path


def handle(msg):
    chat_id = msg["chat"]["id"]
    from_id = str(msg.get("from", {}).get("id", ""))
    mid = msg.get("message_id")

    if from_id not in ALLOWED:
        send(chat_id, f"⛔ Нет доступа. Твой Telegram ID: {from_id}\n"
                      f"Добавь его в DROPBOX_ALLOWED_IDS в /opt/mp-analytics/.env и перезапусти бота.", mid)
        log(f"deny from {from_id}")
        return

    sender = sender_label(msg)
    ts = time.strftime("%Y%m%d_%H%M%S")
    caption = (msg.get("caption") or "").strip()
    text = (msg.get("text") or "").strip()

    # --- команды ---
    if text.lower() in ("/start", "/help", "/старт"):
        send(chat_id, "📥 Это ящик для файлов и скриншотов.\n"
                      "Кидай сюда фото/скриншоты и документы (любой формат) — я сложу их на сервер, "
                      "и в рабочей сессии разберу.\n"
                      "Подпись к файлу (caption) сохраняю рядом как заметку — пиши в ней, что с этим делать.\n\n"
                      "/list — что уже в ящике (последние 20).", mid)
        return
    if text.lower() in ("/list", "/список"):
        try:
            files = sorted(
                (f for f in os.listdir(DROPBOX) if os.path.isfile(os.path.join(DROPBOX, f)) and f != "index.log"),
                key=lambda f: os.path.getmtime(os.path.join(DROPBOX, f)), reverse=True)
        except OSError:
            files = []
        if not files:
            send(chat_id, "Ящик пуст.", mid)
            return
        lines = []
        for f in files[:20]:
            st = os.stat(os.path.join(DROPBOX, f))
            lines.append(f"• {time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}  {f}  ({_human_size(st.st_size)})")
        more = f"\n… ещё {len(files)-20}" if len(files) > 20 else ""
        send(chat_id, "В ящике (свежие сверху):\n" + "\n".join(lines) + more, mid)
        return

    # --- медиа ---
    saved = []   # (имя, размер)
    try:
        photo = msg.get("photo")
        doc = msg.get("document")

        if photo:                                  # скриншот/фото (сжатое) — берём наибольший размер
            big = photo[-1]
            tmp = _uniq(os.path.join(DROPBOX, f"{ts}_{_sanitize(sender,40)}_photo.jpg"))
            size, fp = download_to(big["file_id"], tmp)
            ext = os.path.splitext(fp)[1].lower() or ".jpg"
            if ext != ".jpg":                       # уважаем реальное расширение из Telegram
                new = os.path.splitext(tmp)[0] + ext
                os.rename(tmp, new); tmp = new
            saved.append((os.path.basename(tmp), size))
            _index_line(os.path.basename(tmp), caption or "фото")

        if doc:                                     # документ / файл / картинка «как файл»
            orig = doc.get("file_name") or f"file_{doc['file_id'][:8]}"
            tmp = _uniq(os.path.join(DROPBOX, f"{ts}_{_sanitize(sender,40)}_{_sanitize(orig)}"))
            size, _ = download_to(doc["file_id"], tmp)
            saved.append((os.path.basename(tmp), size))
            _index_line(os.path.basename(tmp), caption or "документ")

        if caption and saved:                       # подпись к файлу → отдельная заметка рядом
            note = os.path.splitext(os.path.join(DROPBOX, saved[0][0]))[0] + ".caption.txt"
            with open(note, "w", encoding="utf-8") as f:
                f.write(caption)

        if not saved:
            if text:                                # просто текст → заметка
                p = save_note(sender, ts, text)
                send(chat_id, f"📝 Заметка сохранена: {os.path.basename(p)}", mid)
            else:
                send(chat_id, "Пришли фото/скриншот или документ — сохраню в ящик.", mid)
            return

        names = "\n".join(f"• {n}  ({_human_size(s)})" for n, s in saved)
        cap = f"\n📝 подпись сохранена как заметка" if caption else ""
        send(chat_id, f"✅ В ящике:\n{names}{cap}", mid)
        log(f"saved {[n for n,_ in saved]} from {from_id}")
    except Exception as e:
        log("handle error: " + traceback.format_exc())
        send(chat_id, f"❌ Не смог сохранить: {type(e).__name__}: {e}", mid)


def main():
    if not TOKEN:
        raise SystemExit("Нет DROPBOX_BOT_TOKEN в /opt/mp-analytics/.env")
    me = api("getMe")["result"]
    log(f"dropbox-bot @{me.get('username')} запущен. allowed={sorted(ALLOWED) or 'ПУСТО (bootstrap)'}")
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            upd = api("getUpdates", params, timeout=60)
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message")
                if m:
                    try:
                        handle(m)
                    except Exception:
                        log("update error: " + traceback.format_exc())
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} на getUpdates; пауза 5с"); time.sleep(5)
        except Exception as e:
            log(f"loop error: {e}; пауза 5с"); time.sleep(5)


if __name__ == "__main__":
    main()

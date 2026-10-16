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
import os, sys, re, stat, json, time, urllib.request, urllib.parse, urllib.error, traceback

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

# --- узкий маршрут ЛЦТ. Пока DROPBOX_LCT_IDS пуст, маршрут неактивен и бот
# --- работает ровно как раньше. ID обязаны входить и в общий allow-list.
LCT_IDS = {x.strip() for x in os.getenv("DROPBOX_LCT_IDS", "").split(",") if x.strip()} & ALLOWED
LCT_INBOX = os.getenv("DROPBOX_LCT_INBOX", "/srv/lct/shared/inbox")
LCT_OUTBOX = os.getenv("DROPBOX_LCT_OUTBOX", "/srv/lct/shared/outbox")
LCT_INDEX = os.path.join(LCT_INBOX, "index.log")
LCT_TAG = re.compile(r"^#lct\b", re.IGNORECASE)        # ровно #lct в начале подписи
LCT_TAG_RU = re.compile(r"^#лцт\b", re.IGNORECASE)     # частая опечатка: та же метка по-русски
OUT_MAX_BYTES = 45 * 1024 * 1024                  # предел Telegram на документ — 50 МБ


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


def send_document(chat_id, blob, filename, reply_to=None):
    """Отдать файл в чат. Multipart собираем руками — зависимостей не добавляем.
    Принимает уже прочитанные байты: файл открывается и проверяется до вызова."""
    boundary = "----dbx" + str(int(time.time() * 1000))
    parts = []

    def field(k, v):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n'.encode())

    field("chat_id", str(chat_id))
    if reply_to:
        field("reply_to_message_id", str(reply_to))
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
                 f'filename="{filename}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(blob)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(f"{API}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def _safe_out_file(name):
    """Путь к выдаче или None. Бот работает от root, поэтому выбора имени из
    listdir мало: отдаём только ОБЫЧНЫЙ файл, физически лежащий в каталоге ЛЦТ.
    Символические ссылки, каталоги, fifo, устройства и имена с путём — мимо."""
    if not name or name.startswith(".") or os.sep in name or (os.altsep and os.altsep in name):
        return None
    base = os.path.realpath(LCT_OUTBOX)
    path = os.path.join(base, name)
    try:
        st = os.lstat(path)                       # lstat: ссылку не разыменовываем
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if os.path.realpath(path) != path:            # путь не уводит за пределы каталога
        return None
    return path


def _open_out_file(name):
    """Прочитать файл выдачи безопасно: (байты, имя) или (None, None).
    Предварительная проверка имени может устареть — участники пишут в outbox, а
    бот работает от root. Поэтому открываем с O_NOFOLLOW (ссылку не откроем) и
    проверяем УЖЕ ОТКРЫТЫЙ объект через fstat. O_NONBLOCK — чтобы fifo не
    подвесил цикл опроса."""
    path = _safe_out_file(name)
    if not path:
        return None, None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        log(f"out open refused {name}: {e.__class__.__name__}")
        return None, None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):          # подменили после проверки — не отдаём
            log(f"out not a regular file after open: {name}")
            return None, None
        if st.st_size > OUT_MAX_BYTES:            # бот от root: не тянем в память что попало
            log(f"out too big: {name} {st.st_size}")
            return None, None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(), os.path.basename(path)
    except OSError as e:
        log(f"out read error {name}: {e}")
        return None, None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _chmod664(path):
    """Каталог ЛЦТ общий для группы lct-work: файл от root должен быть правимым."""
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass


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


def _index_line(name, note, index=None):
    try:
        with open(index or INDEX, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{name}\t{note or ''}\n")
    except OSError:
        pass


def save_note(sender, ts, text, dest=DROPBOX, index=None):
    """Текстовое сообщение без файла → сохраняем как заметку .txt."""
    base = f"{ts}_{_sanitize(sender,40)}_note.txt"
    path = _uniq(os.path.join(dest, base))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    _index_line(os.path.basename(path), "заметка", index)
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

    # Подпись начинается с #lct (для текстовой заметки — сам текст) → файл идёт
    # ТОЛЬКО в каталог ЛЦТ и в общий ящик не попадает: иначе его имя увидят
    # через /list те, кто к ЛЦТ доступа не имеет. Без метки — обычный приём.
    if LCT_TAG_RU.match(caption or text):         # похожая метка кириллицей — не сохраняем нигде
        send(chat_id, "⚠️ Метка набрана по-русски. Используйте #lct в начале подписи. "
                      "Файл не сохранён — отправьте заново с #lct.", mid)
        log(f"ru tag from {from_id}")
        return

    is_lct = bool(LCT_TAG.match(caption or text))
    if is_lct and from_id not in LCT_IDS:         # пустой список ID — тоже отказ
        send(chat_id, "⛔ Подпись начинается с #lct, а доступа к ЛЦТ у вас нет. "
                      "Ничего не сохранил. Уберите метку или попросите добавить вас.", mid)
        log(f"lct tag deny from {from_id}")
        return
    dest, index = (LCT_INBOX, LCT_INDEX) if is_lct else (DROPBOX, INDEX)
    if is_lct:
        try:
            os.makedirs(LCT_INBOX, exist_ok=True)
        except OSError as e:                      # каталога нет → НЕ подменяем его общим ящиком
            send(chat_id, f"❌ Каталог ЛЦТ недоступен, ничего не сохранил: {e}", mid)
            log(f"lct dest unavailable: {e}")
            return

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

    if text and text.lower().split()[0] in ("/out", "/выдай"):
        if from_id not in LCT_IDS:                  # доступ к выдаче ЛЦТ отдельно от общего allow-list
            send(chat_id, "Команда /out доступна только участникам проекта ЛЦТ.", mid)
            log(f"out deny from {from_id}")
            return
        arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        try:
            names = [f for f in os.listdir(LCT_OUTBOX) if _safe_out_file(f)]
        except OSError as e:
            send(chat_id, f"Каталог выдачи недоступен: {e}", mid)
            return
        out = sorted(names, key=lambda f: os.path.getmtime(_safe_out_file(f)), reverse=True)
        if not arg:
            if not out:
                send(chat_id, "Каталог выдачи ЛЦТ пуст.", mid)
                return
            lines = [f"{i+1}. {f}  ({_human_size(os.path.getsize(_safe_out_file(f)))})"
                     for i, f in enumerate(out[:20])]
            send(chat_id, "Готово к выдаче (ЛЦТ):\n" + "\n".join(lines) +
                          "\n\nОтправить: /out <номер или имя>", mid)
            return
        pick = out[int(arg) - 1] if (arg.isdigit() and 1 <= int(arg) <= len(out)) else (arg if arg in out else None)
        blob, fname = _open_out_file(pick) if pick else (None, None)
        if fname is None:                           # не обычный файл, подменён или больше предела
            send(chat_id, "Не отдал: в каталоге выдачи ЛЦТ нет такого обычного файла "
                          "(или он больше 45 МБ). /out — список.", mid)
            return
        try:
            send_document(chat_id, blob, fname, reply_to=mid)
            log(f"sent {pick} to {from_id}")
        except Exception as e:
            send(chat_id, f"❌ Не смог отправить: {type(e).__name__}: {e}", mid)
        return

    # --- медиа ---
    saved = []   # (имя, размер)
    try:
        photo = msg.get("photo")
        doc = msg.get("document")

        if photo:                                  # скриншот/фото (сжатое) — берём наибольший размер
            big = photo[-1]
            tmp = _uniq(os.path.join(dest, f"{ts}_{_sanitize(sender,40)}_photo.jpg"))
            size, fp = download_to(big["file_id"], tmp)
            ext = os.path.splitext(fp)[1].lower() or ".jpg"
            if ext != ".jpg":                       # уважаем реальное расширение из Telegram
                new = os.path.splitext(tmp)[0] + ext
                os.rename(tmp, new); tmp = new
            if is_lct:
                _chmod664(tmp)
            saved.append((os.path.basename(tmp), size))
            _index_line(os.path.basename(tmp), caption or "фото", index)

        if doc:                                     # документ / файл / картинка «как файл»
            orig = doc.get("file_name") or f"file_{doc['file_id'][:8]}"
            tmp = _uniq(os.path.join(dest, f"{ts}_{_sanitize(sender,40)}_{_sanitize(orig)}"))
            size, _ = download_to(doc["file_id"], tmp)
            if is_lct:
                _chmod664(tmp)
            saved.append((os.path.basename(tmp), size))
            _index_line(os.path.basename(tmp), caption or "документ", index)

        if caption and saved:                       # подпись к файлу → отдельная заметка рядом
            note = os.path.splitext(os.path.join(dest, saved[0][0]))[0] + ".caption.txt"
            with open(note, "w", encoding="utf-8") as f:
                f.write(caption)
            if is_lct:
                _chmod664(note)

        if not saved:
            if text:                                # просто текст → заметка
                p = save_note(sender, ts, text, dest, index)
                send(chat_id, f"📝 Заметка сохранена: {os.path.basename(p)}", mid)
            else:
                send(chat_id, "Пришли фото/скриншот или документ — сохраню в ящик.", mid)
            return

        names = "\n".join(f"• {n}  ({_human_size(s)})" for n, s in saved)
        cap = f"\n📝 подпись сохранена как заметка" if caption else ""
        if is_lct:
            send(chat_id, f"✅ Сохранил в каталог ЛЦТ {LCT_INBOX}\n"
                          f"(в общий ящик не попадает):\n{names}{cap}", mid)
        else:
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

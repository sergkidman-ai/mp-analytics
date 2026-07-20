# -*- coding: utf-8 -*-
"""
mail_poller.py — IMAP-поллер: пересланные на выделенный ящик счета → черновик заказа в МойСклад.

Забирает НОВЫЕ (UNSEEN) письма, вытаскивает вложения xls/xlsx/pdf (в т.ч. из пересланных/вложенных
писем), гоняет invoice_to_po.process() с АВТОСОЗДАНИЕМ черновика и шлёт результат в тот же Telegram,
что и бот, — единая лента. Обработанные письма помечает прочитанными.

Зависимостей нет — imaplib/email из stdlib. Конфиг в /opt/mp-analytics/.env:
    MAIL_HOST=imap.yandex.ru
    MAIL_USER=scheta@example.ru
    MAIL_PASS=<пароль приложения>
    MAIL_FOLDER=INBOX
    MAIL_POLL_SEC=60
    TG_NOTIFY_ID=1231747786      # куда слать результаты (по умолчанию — первый из TG_ALLOWED_IDS)
    TG_BOT_TOKEN=...             # тот же бот для отправки

Запуск: python mail_poller.py   (в бою — под systemd, см. invoice-mail.service)
"""
import os, sys, time, imaplib, email, json, urllib.request, traceback
from email.header import decode_header

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
import invoice_to_po as pipe          # счёт → «Заказ поставщику»
import upd_to_supply as upd_pipe       # УПД  → «Приёмка»

HOST = os.getenv("MAIL_HOST", "").strip()
USER = os.getenv("MAIL_USER", "").strip()
PASS = os.getenv("MAIL_PASS", "").strip()
FOLDER = os.getenv("MAIL_FOLDER", "INBOX").strip()          # папка счетов → движок заказа
FOLDER_UPD = os.getenv("MAIL_FOLDER_UPD", "").strip()       # папка УПД → движок приёмки (пусто = выкл)
POLL = int(os.getenv("MAIL_POLL_SEC", "60"))
TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
NOTIFY = [x.strip() for x in os.getenv("TG_NOTIFY_ID", "").split(",") if x.strip()] or \
    [x.strip() for x in os.getenv("TG_ALLOWED_IDS", "").split(",") if x.strip()]
INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox_mail")
os.makedirs(INBOX, exist_ok=True)
OK_EXT = (".xls", ".xlsx", ".pdf", ".xml", ".zip")


def log(m): print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


def imap_utf7_encode(name):
    """UTF-8 имя папки → IMAP modified UTF-7 (RFC 3501) для SELECT кириллических папок."""
    import base64
    res = []; i = 0; n = len(name)
    while i < n:
        ch = name[i]
        if ch == "&":
            res.append("&-"); i += 1
        elif "\x20" <= ch <= "\x7e":
            res.append(ch); i += 1
        else:
            j = i
            while j < n and not ("\x20" <= name[j] <= "\x7e"):
                j += 1
            chunk = name[i:j].encode("utf-16-be")
            b = base64.b64encode(chunk).decode("ascii").rstrip("=").replace("/", ",")
            res.append("&" + b + "-"); i = j
    return "".join(res)


def tg_send(text):
    if not (TOKEN and NOTIFY):
        return
    text = text if len(text) <= 4000 else text[:3990] + "\n…(обрезано)"
    for chat in NOTIFY:
        data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            log(f"tg_send error to {chat}: {e}")


def _dec(s):
    if not s:
        return ""
    out = []
    for part, enc in decode_header(s):
        out.append(part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part)
    return "".join(out)


def attachments(msg):
    """Все вложения xls/xlsx/pdf, включая вложенные пересланные письма. → [(filename, bytes)]."""
    res = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = _dec(part.get_filename())
        if fn and fn.lower().endswith(OK_EXT):
            payload = part.get_payload(decode=True)
            if payload:
                res.append((fn, payload))
    return res


def process_message(m, num, engine, kind):
    subj = _dec(m.get("Subject"))
    frm = _dec(m.get("From"))
    atts = attachments(m)
    if not atts:
        log(f"письмо {num.decode()} без вложений: {subj[:60]}")
        return
    log(f"письмо {num.decode()} [{kind}] от {frm[:40]} «{subj[:50]}» — вложений: {len(atts)}")
    for fn, data in atts:
        safe = f"{int(time.time())}_{os.path.basename(fn)}"
        path = os.path.join(INBOX, safe)
        open(path, "wb").write(data)
        res = engine.process(path, create=True)
        report = engine.format_report(res)
        head = f"📧 Из почты ({kind}) · {frm[:40]}\nФайл: {fn}\n\n"
        tg_send(head + report)
        log(f"  {fn}: ok={res.get('ok')} created={res.get('created')} stop={res.get('stop')} err={res.get('error')}")


def poll_once(imap, folder_name, engine, kind):
    # кириллические/с пробелом имена папок кодируем в mUTF-7 и оборачиваем в кавычки
    folder = folder_name if folder_name.isascii() else imap_utf7_encode(folder_name)
    typ, _ = imap.select(f'"{folder}"')
    if typ != "OK":
        log(f"не удалось выбрать папку «{folder_name}»"); return
    typ, dat = imap.search(None, "UNSEEN")
    if typ != "OK":
        return
    nums = dat[0].split()
    for num in nums:
        # PEEK — не помечаем прочитанным до обработки
        typ, msgdat = imap.fetch(num, "(BODY.PEEK[])")
        if typ != "OK":
            continue
        raw = msgdat[0][1]
        try:
            m = email.message_from_bytes(raw)
            process_message(m, num, engine, kind)
        except Exception:
            log("process_message error: " + traceback.format_exc())
        # помечаем прочитанным после обработки (в т.ч. при ошибке — чтобы не зациклить)
        imap.store(num, "+FLAGS", "\\Seen")


def main():
    for k, v in (("MAIL_HOST", HOST), ("MAIL_USER", USER), ("MAIL_PASS", PASS)):
        if not v:
            raise SystemExit(f"Нет {k} в /opt/mp-analytics/.env")
    # (папка, движок, метка) — счета всегда; УПД-папка если задана MAIL_FOLDER_UPD
    routes = [(FOLDER, pipe, "Счёт→Заказ")]
    if FOLDER_UPD:
        routes.append((FOLDER_UPD, upd_pipe, "УПД→Приёмка"))
    log(f"mail-poller запущен: {USER}@{HOST}, интервал {POLL}с, notify={NOTIFY or 'НЕТ'}, "
        f"папки: {', '.join(f'{f} [{k}]' for f, _, k in routes)}")
    while True:
        try:
            imap = imaplib.IMAP4_SSL(HOST)
            imap.login(USER, PASS)
            try:
                for folder_name, engine, kind in routes:
                    poll_once(imap, folder_name, engine, kind)
            finally:
                try: imap.logout()
                except Exception: pass
        except Exception as e:
            log(f"IMAP error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()

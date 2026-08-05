# поток: prc
# -*- coding: utf-8 -*-
"""
Забор прайса из почтовой папки поставщика (IMAP, тот же ящик, что счета и УПД).

Берём ПОСЛЕДНЕЕ по дате письмо с подходящим вложением. Письма не помечаем прочитанными:
загрузка идемпотентна и повторный прогон за день должен видеть тот же файл.
"""
import os
import base64
import email
import imaplib
from email.header import decode_header, make_header

from dotenv import load_dotenv

load_dotenv(os.getenv("MP_ENV_PATH", "/opt/mp-analytics/.env"))

PRICE_EXT = (".xlsx", ".xls", ".csv")


def imap_utf7(name):
    """UTF-8 имя папки -> modified UTF-7 (RFC 3501): без него кириллические папки не выбрать."""
    out, i = [], 0
    while i < len(name):
        char = name[i]
        if char == "&":
            out.append("&-")
            i += 1
        elif "\x20" <= char <= "\x7e":
            out.append(char)
            i += 1
        else:
            j = i
            while j < len(name) and not ("\x20" <= name[j] <= "\x7e"):
                j += 1
            chunk = base64.b64encode(name[i:j].encode("utf-16-be")).decode()
            out.append("&" + chunk.rstrip("=").replace("/", ",") + "-")
            i = j
    return "".join(out)


def _hdr(value):
    return str(make_header(decode_header(value or "")))


def connect():
    box = imaplib.IMAP4_SSL(os.getenv("MAIL_HOST"))
    box.login(os.getenv("MAIL_USER"), os.getenv("MAIL_PASS"))
    return box


def fetch_latest_price(folder, extensions=PRICE_EXT):
    """Последнее письмо папки с вложением-прайсом.

    Возвращает dict: filename, content (bytes), subject, date, uid — или None, если писем нет.
    """
    box = connect()
    try:
        status, _ = box.select('"%s"' % imap_utf7(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP: не открылась папка {folder!r}")
        status, data = box.search(None, "ALL")
        uids = data[0].split()
        for uid in reversed(uids):                    # с конца: свежие письма последние
            _, raw = box.fetch(uid, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
            for part in msg.walk():
                name = part.get_filename()
                if not name:
                    continue
                name = _hdr(name)
                if not name.lower().endswith(tuple(extensions)):
                    continue
                return {
                    "filename": name,
                    "content": part.get_payload(decode=True),
                    "subject": _hdr(msg.get("Subject")),
                    "date": msg.get("Date"),
                    "uid": uid.decode(),
                }
        return None
    finally:
        try:
            box.logout()
        except Exception:
            pass

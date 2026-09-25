# поток: prc
# -*- coding: utf-8 -*-
"""
Забор прайса из почтовой папки поставщика (IMAP, тот же ящик, что счета и УПД).

Берём ПОСЛЕДНЕЕ по дате письмо с подходящим вложением; профилю с `pick_by_content`
файл выбираем по содержимому, а не по очереди (см. `_pick_by_content`). Письма не помечаем прочитанными:
загрузка идемпотентна и повторный прогон за день должен видеть тот же файл.
"""
import os
import base64
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

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


def mark_seen(folder, imap_uid):
    """Пометить письмо прочитанным. Человек видит в почте, какие прайсы уже забраны.

    Адресуем по UID, а не по номеру письма в выборке: номера сдвигаются, когда в папку
    приходит или из неё удаляется письмо, и «прочитано» уехало бы на чужое. Поиск по
    Message-ID тоже не годится — сервер отвечает «[UNAVAILABLE] SEARCH Backend error».
    Возвращает True, если флаг поставлен; молчаливое False — почта не наш рабочий инструмент,
    из-за неё загрузка прайса падать не должна.
    """
    if not imap_uid:
        return False
    box = connect()
    try:
        status, _ = box.select('"%s"' % imap_utf7(folder))     # без readonly: будем писать флаг
        if status != "OK":
            return False
        status, data = box.uid("STORE", str(imap_uid), "+FLAGS", "\\Seen")
        return status == "OK" and bool(data) and data[0] is not None
    except Exception:
        return False
    finally:
        try:
            box.logout()
        except Exception:
            pass


PICK_DEPTH = 4          # сколько последних писем смотрим, когда выбираем файл по содержимому


def _pick_by_content(found, profile):
    """Из писем ОДНОГО дня берём файл, в котором профиль разобрал больше строк с остатком.

    Разбор дешёвый (тот же, что на загрузке) и делается на уже скачанных вложениях —
    в почту повторно не ходим. Ничего не разобралось нигде — возвращаем самое свежее
    письмо, то есть прежнее поведение: решение о загрузке принимает загрузчик, а не почта.
    """
    from .parser import parse       # локальный импорт: parser тянет профили, кругового не делаем

    def day(letter):
        try:
            return parsedate_to_datetime(letter["date"]).date()
        except Exception:
            return None

    top = day(found[0])
    same = [x for x in found if top and day(x) == top] or found[:1]
    if len(same) == 1:
        return same[0]
    best, best_rows = same[0], -1                     # same идёт от свежего: при равенстве
    for letter in same:                               # остаётся свежее письмо
        try:
            rows, _ = parse(letter["content"], profile)
            n = sum(1 for r in rows if r.get("qty") is not None)
        except Exception:
            n = 0
        letter["pick_rows"] = n
        if n > best_rows:
            best, best_rows = letter, n
    return best


def fetch_latest_price(folder, extensions=PRICE_EXT, pattern=None, profile=None):
    """Последнее письмо папки с вложением-прайсом.

    `pattern` — регулярка на ИМЯ файла, нужна, когда в письме несколько прайсов. Реальный
    случай: Феррет присылает одним письмом «Прайслист Cactus …xlsx» (наш) и «Прайслист
    Оригинал.xls» (оригинальные картриджи, другой ассортимент) — без фильтра берётся первое
    попавшееся вложение, то есть не то.

    `profile` нужен профилям с `pick_by_content` (Сакура): имена файлов у них совпадают,
    и письмо выбирается по содержимому. Без профиля поведение прежнее — последнее письмо.

    Возвращает dict: filename, content (bytes), subject, date, uid — или None, если писем нет.
    """
    rx = re.compile(pattern, re.I) if pattern else None
    by_content = bool(profile is not None and getattr(profile, "pick_by_content", False))
    found = []
    box = connect()
    try:
        status, _ = box.select('"%s"' % imap_utf7(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"IMAP: не открылась папка {folder!r}")
        status, data = box.search(None, "ALL")
        uids = data[0].split()
        for uid in reversed(uids):                    # с конца: свежие письма последние
            # Вместе с телом просим UID: номер письма (`uid` здесь — порядковый) живёт только
            # в этой выборке, а пометить прочитанным письмо нужно уже в другом соединении.
            _, raw = box.fetch(uid, "(UID RFC822)")
            head = raw[0][0] if isinstance(raw[0], tuple) else b""
            imap_uid = (re.search(rb"UID (\d+)", head or b"") or [None, b""])[1].decode()
            msg = email.message_from_bytes(raw[0][1])
            letter = None
            for part in msg.walk():
                name = part.get_filename()
                if not name:
                    continue
                name = _hdr(name)
                if not name.lower().endswith(tuple(extensions)):
                    continue
                if rx and not rx.search(name):
                    continue
                letter = {
                    "filename": name,
                    "content": part.get_payload(decode=True),
                    "subject": _hdr(msg.get("Subject")),
                    "date": msg.get("Date"),
                    "uid": uid.decode(),              # номер в выборке — только для отладки
                    "imap_uid": imap_uid,             # стабильный UID — им и метим прочитанным
                    "message_id": (msg.get("Message-ID") or "").strip(),
                }
                break                                 # одно вложение-прайс с письма
            if letter is None:
                continue
            if not by_content:
                return letter                         # обычный случай: последнее письмо
            found.append(letter)
            if len(found) >= PICK_DEPTH:
                break
        return _pick_by_content(found, profile) if found else None
    finally:
        try:
            box.logout()
        except Exception:
            pass

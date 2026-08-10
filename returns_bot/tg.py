# поток: ret
"""Тонкая обёртка над Bot API для бота возвратов.

Свой токен (`TG_RETURNS_BOT_TOKEN`): два бота на одном токене дерутся за getUpdates.
"""
import json
import os
import time

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

LIMIT = 3900          # у Telegram 4096, оставляем запас на HTML-теги
TIMEOUT = 60


def token():
    t = os.getenv("TG_RETURNS_BOT_TOKEN")
    if not t:
        raise RuntimeError("нет TG_RETURNS_BOT_TOKEN в .env (бот заводится у @BotFather)")
    return t


def allowed_ids():
    raw = os.getenv("TG_RETURNS_ALLOWED_IDS", "") or ""
    return {c.strip() for c in raw.split(",") if c.strip()}


def notify_ids():
    raw = os.getenv("TG_RETURNS_NOTIFY_ID", "") or ""
    ids = [c.strip() for c in raw.split(",") if c.strip()]
    return ids or sorted(allowed_ids())


def _api(method, data=None, files=None, tries=4):
    url = f"https://api.telegram.org/bot{token()}/{method}"
    for n in range(tries):
        try:
            r = requests.post(url, data=data, files=files, timeout=TIMEOUT)
            j = r.json()
            if j.get("ok"):
                return j.get("result")
            # 429: Telegram сам говорит, сколько ждать
            wait = ((j.get("parameters") or {}).get("retry_after")) or 2 ** n
            if r.status_code in (420, 429) or r.status_code >= 500:
                time.sleep(min(wait, 30))
                continue
            raise RuntimeError(f"{method}: {j.get('description')}")
        except requests.RequestException:
            if n == tries - 1:
                raise
            time.sleep(2 ** n)
    raise RuntimeError(f"{method}: не удалось за {tries} попыток")


def split(text, limit=LIMIT):
    """Режем по границам блоков (пустая строка), затем по строкам."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for block in text.split("\n\n"):
        piece = (buf + "\n\n" + block) if buf else block
        if len(piece) <= limit:
            buf = piece
            continue
        if buf:
            out.append(buf)
        if len(block) <= limit:
            buf = block
            continue
        buf = ""
        for line in block.split("\n"):
            cand = (buf + "\n" + line) if buf else line
            if len(cand) <= limit:
                buf = cand
            else:
                out.append(buf)
                buf = line[:limit]
    if buf:
        out.append(buf)
    return out


def send(chat_id, text, parse_mode="HTML", reply_markup=None):
    """Длинный текст режется; клавиатура вешается на ПОСЛЕДНИЙ кусок — кнопка внизу сводки."""
    parts = split(text)
    for n, part in enumerate(parts):
        data = {"chat_id": chat_id, "text": part, "parse_mode": parse_mode,
                "disable_web_page_preview": "true"}
        if reply_markup and n == len(parts) - 1:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        _api("sendMessage", data)
        time.sleep(0.2)


def answer_callback(callback_query_id, text=None):
    """Погасить «часики» на кнопке. Telegram ждёт ответ ~10 с, поэтому зовём ДО долгой работы."""
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text[:200]
    try:
        return _api("answerCallbackQuery", data, tries=1)
    except Exception:
        return None      # протухший callback (>10 с) — не повод ронять обработку


def send_photo(chat_id, png_bytes, caption=None, filename="code.png"):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption[:1000]
        data["parse_mode"] = "HTML"
    return _api("sendPhoto", data, files={"photo": (filename, png_bytes, "image/png")})


def send_document(chat_id, data, filename, caption=None, mime="image/png"):
    """Файл БЕЗ пережатия.

    Штрихкод шлём именно так: `sendPhoto` перегоняет картинку в JPEG, тонкие штрихи плывут
    и сканер в пункте выдачи их не берёт (проверено на живом коде 07.08.2026).
    """
    d = {"chat_id": chat_id}
    if caption:
        d["caption"] = caption[:1000]
        d["parse_mode"] = "HTML"
    return _api("sendDocument", d, files={"document": (filename, data, mime)})


def get_updates(offset=None, timeout=50):
    data = {"timeout": timeout}
    if offset is not None:
        data["offset"] = offset
    return _api("getUpdates", data, tries=2) or []

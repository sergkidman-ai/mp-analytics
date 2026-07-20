# -*- coding: utf-8 -*-
"""
tg_bot.py — Telegram-бот приёма счетов → черновик «Заказ поставщику» в МойСклад.

Пересылаешь боту файл счёта (xls/xlsx/pdf) → бот прогоняет invoice_to_po.process()
с АВТОСОЗДАНИЕМ черновика и отвечает разбором + ссылкой на заказ в МС.

Зависимостей нет — long-polling на urllib (как ms.py). Токен и список разрешённых
Telegram-ID берём из /opt/mp-analytics/.env:
    TG_BOT_TOKEN=123456:AA...
    TG_ALLOWED_IDS=11111111,22222222      # numeric user id, через запятую

Пока TG_ALLOWED_IDS пуст или ID не в списке — бот НЕ обрабатывает, а сообщает твой ID,
чтобы ты вписал его в .env (безопасный bootstrap allow-list).

Запуск: python tg_bot.py   (в бою — под systemd, см. invoice-bot.service)
"""
import os, sys, re, json, time, urllib.request, urllib.parse, urllib.error, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
import invoice_to_po as pipe          # счёт → «Заказ поставщику»
import upd_to_supply as upd_pipe       # УПД  → «Приёмка»

# Роутинг по имени файла: «УПД»/«upd»/имя ЭДО-титула или .xml/.zip → приёмка, иначе → заказ по счёту.
UPD_RE = re.compile(r"упд|upd|nschfdoppr", re.I)

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
ALLOWED = {x.strip() for x in os.getenv("TG_ALLOWED_IDS", "").split(",") if x.strip()}
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox")
os.makedirs(INBOX, exist_ok=True)
OK_EXT = (".xls", ".xlsx", ".pdf", ".xml", ".zip")


def api(method, params=None, timeout=60):
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def send(chat_id, text, reply_to=None):
    # Telegram лимит 4096 симв.
    text = text if len(text) <= 4000 else text[:3990] + "\n…(обрезано)"
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    try:
        api("sendMessage", p)
    except Exception as e:
        log(f"sendMessage error: {e}")


def download_file(file_id, dst_name):
    info = api("getFile", {"file_id": file_id})
    fp = info["result"]["file_path"]
    dst = os.path.join(INBOX, dst_name)
    with urllib.request.urlopen(f"{FILE_API}/{fp}", timeout=120) as r:
        open(dst, "wb").write(r.read())
    return dst


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def handle(msg):
    chat_id = msg["chat"]["id"]
    from_id = str(msg.get("from", {}).get("id", ""))
    mid = msg.get("message_id")

    # allow-list (и bootstrap)
    if from_id not in ALLOWED:
        send(chat_id, f"⛔ Нет доступа. Твой Telegram ID: {from_id}\n"
                      f"Добавь его в TG_ALLOWED_IDS в /opt/mp-analytics/.env и перезапусти бота.", mid)
        log(f"deny from {from_id}")
        return

    doc = msg.get("document")
    if not doc:
        if (msg.get("text") or "").startswith("/"):
            send(chat_id, "Пришли файл:\n"
                          "• счёт (xls / xlsx / pdf) → создам черновик «Заказ поставщику»;\n"
                          "• УПД (xls / xlsx с «УПД»/«upd» в имени) → создам «Приёмку» на основании заказа;\n"
                          "• УПД из ЭДО/Диадока (.xml или .zip выгрузки «в исходном формате») → тоже «Приёмку».\n"
                          "Пришлю ссылку; всё, что требует проверки, будет в комментарии/предупреждениях.", mid)
        else:
            send(chat_id, "Жду файл: счёт или УПД (xls / xlsx / pdf).", mid)
        return

    fname = doc.get("file_name") or f"invoice_{doc['file_id'][:8]}"
    if not fname.lower().endswith(OK_EXT):
        send(chat_id, f"Формат «{fname}» не поддержан. Нужен xls, xlsx или pdf.", mid)
        return

    is_upd = bool(UPD_RE.search(fname)) or fname.lower().endswith((".xml", ".zip"))
    engine = upd_pipe if is_upd else pipe
    kind = "УПД → Приёмка" if is_upd else "Счёт → Заказ"
    send(chat_id, f"⏳ Принял «{fname}» ({kind}), обрабатываю…", mid)
    try:
        # уникализируем имя файла, чтобы параллельные файлы не перетёрли друг друга
        safe = f"{int(time.time())}_{os.path.basename(fname)}"
        path = download_file(doc["file_id"], safe)
        log(f"process {safe} from {from_id} [{'UPD' if is_upd else 'INVOICE'}]")
        res = engine.process(path, create=True)
        send(chat_id, engine.format_report(res), mid)
        log(f"done {safe}: ok={res.get('ok')} created={res.get('created')} stop={res.get('stop')} err={res.get('error')}")
    except Exception as e:
        log("handle error: " + traceback.format_exc())
        send(chat_id, f"❌ Внутренняя ошибка обработки: {type(e).__name__}: {e}", mid)


def main():
    if not TOKEN:
        raise SystemExit("Нет TG_BOT_TOKEN в /opt/mp-analytics/.env")
    me = api("getMe")["result"]
    log(f"bot @{me.get('username')} запущен. allowed={sorted(ALLOWED) or 'ПУСТО (bootstrap)'}")
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

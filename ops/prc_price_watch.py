# поток: prc — сторож почты: пришёл прайс поставщика → сразу оприходование и вся цепочка
"""Сторож прайсов: увидел в почте НОВОЕ письмо с прайсом — прогнал загрузку и отчитался.

Зачем отдельный сторож, а не таймер «раз в сутки в 9:00»: поставщик присылает прайс когда
угодно и иногда переприсылает исправленный тем же днём. Расписание по часам либо опоздает
на полдня, либо повторно перезальёт то же самое. Поэтому таймер только будит сторожа, а
решение «грузить или нет» принимает письмо: грузим ровно тогда, когда в папке появилось
письмо свежее уже обработанного.

Состояние — свой файл в `logs/`, а НЕ журнал `prc_price_load`: загрузка, отменённая
проверкой аномалий, до журнала не доходит, и по журналу сторож считал бы такое письмо
вечно новым и дёргал бы Сергея каждые полчаса.

Цепочка после загрузки живёт внутри `prices.run`: отчёты в `docs/prc/`, сверка новинок
с каталогом (вкладка «Новинки»), лист ожидания комплектов. Сторож добавляет только
последнее звено — сказать человеку, что пришло и что ждёт его рук.

Запуск: `./venv/bin/python -m ops.prc_price_watch --supplier kaktus_msk`
        `--dry` — прогон без записи в МС, `--force` — грузить, даже если письмо не новое.
"""
import argparse
import io
import json
import os
import sys
import contextlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

import requests

from prices.profiles import get_profile
from prices.mailbox import fetch_latest_price

LOG_DIR = Path("/opt/mp-analytics/logs")
# Сергей = 1031321444 и бот @Pro_Dropbox_bot: в TG_NOTIFY_ID лежат другие люди,
# туда прайсовые сводки слать нельзя (память project_mp_telegram_channels).
NOTIFY_ID = os.getenv("PRC_NOTIFY_ID", "1031321444")
TG_TOKEN = os.getenv("DROPBOX_BOT_TOKEN", "").strip()
TG_LIMIT = 3900


def log(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {text}\n")


def tg(text):
    """Сводка Сергею. Молчать при сбое телеграма нельзя — но и падать из-за него тоже."""
    if not TG_TOKEN:
        return "нет DROPBOX_BOT_TOKEN"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": NOTIFY_ID, "text": text[:TG_LIMIT],
                                "disable_web_page_preview": "true"}, timeout=60)
        return "ok" if r.ok else f"telegram {r.status_code}: {r.text[:200]}"
    except Exception as exc:                                  # сеть/таймаут — не роняем прогон
        return f"telegram: {type(exc).__name__}: {exc}"


def state_path(key):
    return LOG_DIR / f"prc_price_watch_{key}.json"


def read_state(key):
    try:
        return json.loads(state_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(key, letter, result):
    state_path(key).parent.mkdir(parents=True, exist_ok=True)
    state_path(key).write_text(json.dumps({
        "filename": letter["filename"], "date": letter["date"], "subject": letter["subject"],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "result": result,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def letter_dt(letter):
    """Дата письма как datetime. Без даты (кривой заголовок) — считаем письмо свежим."""
    try:
        return parsedate_to_datetime(letter["date"])
    except (TypeError, ValueError):
        return None


def is_new(letter, state):
    """Письмо новее обработанного? Ключ — дата отправки плюс имя файла.

    Порядковый номер письма из IMAP ключом не годится: он меняется, когда из папки что-то
    удаляют. Имя файла в паре с датой ловит и обычный «новый день — новый прайс», и
    переприсланный исправленный прайс с тем же именем.
    """
    if not state:
        return True
    old, new = state.get("date"), letter.get("date")
    old_dt = parsedate_to_datetime(old) if old else None
    new_dt = letter_dt(letter)
    if old_dt is None or new_dt is None:
        return letter["filename"] != state.get("filename") or new != old
    if new_dt > old_dt:
        return True
    return new_dt == old_dt and letter["filename"] != state.get("filename")


def run_load(key, dry):
    """Прогон загрузки. -> (текст вывода, код). SystemExit — это отказ по правилу, не сбой."""
    from prices import run as prices_run
    argv = ["--supplier", key] + ([] if dry else ["--apply"])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = prices_run.main(argv)
    except SystemExit as exc:
        return buf.getvalue() + f"\nОТМЕНА: {exc}", 2
    except Exception as exc:
        return buf.getvalue() + f"\nСБОЙ: {type(exc).__name__}: {exc}", 3
    return buf.getvalue(), code or 0


def message(profile, letter, out, code, dry):
    """Сводка человеку: что пришло, что легло, что ждёт его рук."""
    head = {0: "✅ загружено", 2: "⛔ загрузка ОТМЕНЕНА", 3: "❌ сбой"}.get(code, "⚠ ошибка")
    if dry and code == 0:
        head = "🔎 сухой прогон"
    keep = [ln for ln in out.splitlines()
            if ln.strip() and not ln.startswith(("  документ ", "  удалён ", "  карточка "))]
    return (f"{head} — {profile.title}\nПисьмо: {letter['subject'] or '—'} / {letter['date']}\n\n"
            + "\n".join(keep[-28:]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сторож почты: новый прайс -> оприходование")
    ap.add_argument("--supplier", default="kaktus_msk")
    ap.add_argument("--dry", action="store_true", help="прогон без записи в МойСклад")
    ap.add_argument("--force", action="store_true", help="грузить, даже если письмо не новое")
    ap.add_argument("--quiet", action="store_true", help="не слать телеграм")
    args = ap.parse_args(argv)

    profile = get_profile(args.supplier)
    logfile = LOG_DIR / f"prc_price_watch_{profile.key}.log"

    try:
        letter = fetch_latest_price(profile.mail_folder, pattern=profile.file_pattern)
    except Exception as exc:
        log(logfile, f"ПОЧТА НЕДОСТУПНА: {type(exc).__name__}: {exc}")
        return 3                                  # молча: почта моргает, будить человека нечем
    if not letter:
        log(logfile, f"в папке «{profile.mail_folder}» писем с прайсом нет")
        return 0

    state = read_state(profile.key)
    if not (args.force or is_new(letter, state)):
        log(logfile, f"письмо не новое ({letter['filename']}, {letter['date']}) — пропуск")
        return 0

    log(logfile, f"новое письмо: {letter['filename']} / {letter['date']} — запускаю загрузку"
                 + (" (сухой прогон)" if args.dry else ""))
    out, code = run_load(profile.key, args.dry)
    log(logfile, f"итог {code}\n" + out)
    # Состояние пишем при ЛЮБОМ исходе: отменённую по аномалиям загрузку человек разбирает
    # руками, а сторож не должен долбить его тем же письмом каждые полчаса.
    if not args.dry:
        write_state(profile.key, letter, code)
    if not args.quiet:
        log(logfile, "телеграм: " + tg(message(profile, letter, out, code, args.dry)))
    return code


if __name__ == "__main__":
    sys.exit(main())

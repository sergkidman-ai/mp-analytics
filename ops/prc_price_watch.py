# поток: prc — сторож почты: пришёл прайс поставщика → сразу оприходование и вся цепочка
"""Сторож прайсов: увидел в почте НОВОЕ письмо с прайсом — прогнал загрузку и отчитался.

Зачем отдельный сторож, а не таймер «раз в сутки в 9:00»: поставщик присылает прайс когда
угодно и иногда переприсылает исправленный тем же днём. Расписание по часам либо опоздает
на полдня, либо повторно перезальёт то же самое. Поэтому таймер только будит сторожа, а
решение «грузить или нет» принимает письмо: грузим ровно тогда, когда в папке появилось
письмо свежее уже обработанного.

Обратная сторона: прайс приходит раз в день, и после созданного оприходования ходить в почту
до вечера незачем — сторож ставит замок на сутки (`done_today`). Замок снимает удачная
загрузка ТОЛЬКО сегодняшнего письма; после отмены по аномалиям слежка продолжается, чтобы
поймать исправленный прайс того же дня.

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
import re
import sys
import contextlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

import requests

from core.db import query, upsert
from prices import mail_schedule, unprocessed
from prices.profiles import get_profile
from prices.mailbox import fetch_latest_price

LOG_DIR = Path("/opt/mp-analytics/logs")
# Свой бот потока — @ds_prc_bot (TG_PRC_BOT_TOKEN), адресат Сергей = 1031321444.
# В TG_NOTIFY_ID лежат другие люди, туда прайсовые сводки слать нельзя
# (память project_mp_telegram_channels).
NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
TG_LIMIT = 3900
# Почта моргает; будить человека первым же таймаутом незачем, а молчать сутки — нельзя.
MAIL_ALERT_AFTER = 3                                   # подряд неудач ≈ 1.5 часа без почты
MSK = timezone(timedelta(hours=3))                     # часы сервера UTC, сутки считаем по Москве


def log(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {text}\n")


def tg(text):
    """Сводка Сергею. Молчать при сбое телеграма нельзя — но и падать из-за него тоже."""
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    out = []
    for chat in NOTIFY_IDS:                # адресатов может быть несколько (список в .env)
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": chat, "text": text[:TG_LIMIT],
                                    "disable_web_page_preview": "true"}, timeout=60)
            out.append(f"{chat}: " + ("ok" if r.ok else f"{r.status_code} {r.text[:120]}"))
        except Exception as exc:                              # сеть/таймаут — не роняем прогон
            out.append(f"{chat}: {type(exc).__name__}: {exc}")
    return "; ".join(out)


# Поставщики БЕЗ прайса. Булата (и дальше ВТТ, Рамис, Блоссом) грузит по API внешний
# загрузчик, а нам письмом приходит только список несопоставленного — его разбирает
# `prices.unprocessed`, и кончается он вкладкой «Новинки», а не оприходованием в МС.
# Профиля в `prices/profiles.py` у такого поставщика нет и быть не может (нет ни колонок,
# ни документов), поэтому собираем сторожу ровно то, чем он пользуется: папка, шаблон
# вложения, расширение вложения и имя для сводки.
UNPROCESSED = {"bulat": "Булат", "s_print_msk": "Солюшнс принт МСК",
               "rapid": "Рапид (SuperFine)", "easy_print": "Тонероптторг (Изипринт)"}


def profile_of(key):
    """Профиль поставщика — настоящий или заменитель для писем «необработанные товары МС»."""
    if key in UNPROCESSED:
        return SimpleNamespace(key=key, title=UNPROCESSED[key], unprocessed=True,
                               mail_folder=unprocessed.FOLDER,
                               file_pattern=unprocessed.SUPPLIERS[key][0],
                               extensions=(".txt",))
    return get_profile(key)


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


def done_today(state):
    """Сегодняшний прайс этого поставщика уже превращён в оприходование?

    Решение Сергея 12.08.2026: поставщик присылает прайс один раз в день, и после созданного
    оприходования смотреть его почту сегодня незачем. Поэтому при удачной загрузке письма
    СЕГОДНЯШНЕГО дня (по Москве) сторож до завтра в почту не ходит.

    Считаем только удачу (`result == 0`): отменённую по аномалиям загрузку разбирает человек,
    и переприсланный тем же днём исправленный прайс поймать надо — за ним и следим дальше.
    Обойти замок можно `--force`.
    """
    if not state or state.get("result") != 0:
        return False
    dt = letter_dt(state)
    return dt is not None and dt.astimezone(MSK).date() == datetime.now(MSK).date()


def note_arrival(profile, letter):
    """Дата письма — в историю приходов: по ней сторож учит, когда этого поставщика ждать.

    Письмо сторож и так забирает каждый прогон, отдельного похода в почту здесь нет.
    За день остаётся ПЕРВОЕ письмо (`DO NOTHING`): у Булата и Солюшнс принта их несколько,
    а срок ожидания считается по первому.
    """
    dt = letter_dt(letter)
    if dt is None:
        return
    dt = dt.astimezone(MSK)
    upsert("prc_mail_arrival",
           [{"supplier_key": profile.key, "letter_date": dt.date(), "first_at": dt,
             "subject": (letter.get("subject") or "")[:300],
             "filename": (letter.get("filename") or "")[:200]}],
           ["supplier_key", "letter_date"], update_cols=[])


def overdue_text(profile, late):
    """Сообщение о просрочке. Три строки: кто, когда обычно ждём, сколько уже молчит."""
    lines = [f"⏰ {profile.title} — прайса сегодня нет.",
             f"Обычно приходит к {mail_schedule.hhmm(late['median'])} — "
             f"ждали до {mail_schedule.hhmm(late['deadline'])}.",
             f"Последний: {late['last_date']:%d.%m} {mail_schedule.hhmm(late['last_time'])} — "
             f"молчит {late['silent']} раб. дн."]
    # Молчат ВСЕ разом — это не поставщик, это праздник или наша почта. Иначе в такой день
    # прилетело бы шесть одинаковых пингов, и каждый увёл бы не туда.
    if not query("""select 1 from prc_mail_arrival where letter_date = %s limit 1""",
                 (late["today"],)):
        lines.append("Сегодня нет писем НИ ОТ КОГО — похоже на праздник или нашу почту.")
    return "\n".join(lines)


def overdue_alert(profile, logfile, quiet=False, check_only=False):
    """Прайс просрочен -> одно сообщение в сутки. -> текст сообщения или None.

    Замок на сутки — отдельным файлом, как у недоступной почты: состояние загрузки
    (`write_state`) переписывается только при удачном письме, а тревожить надо ровно тогда,
    когда письма и нет.
    """
    late = mail_schedule.overdue(profile.key)
    if not late:
        return None
    text = overdue_text(profile, late)
    if check_only:
        return text
    lock = LOG_DIR / f"prc_price_watch_{profile.key}_overdue.txt"
    if lock.exists() and lock.read_text().strip() == str(late["today"]):
        return None
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(late["today"]))
    log(logfile, f"ПРОСРОЧКА: молчит {late['silent']} раб. дн., срок "
                 f"{mail_schedule.hhmm(late['deadline'])}")
    if not quiet:
        log(logfile, "телеграм: " + tg(text))
    return text


def run_load(profile, dry):
    """Прогон загрузки. -> (текст вывода, код). SystemExit — это отказ по правилу, не сбой.

    У поставщика без прайса дорога другая — `prices.unprocessed`: в МойСклад не пишется
    ничего, строки ложатся во вкладку «Новинки». Флаг `--apply` значит там ровно то же
    самое («писать»), поэтому сухой прогон устроен одинаково.
    """
    if getattr(profile, "unprocessed", False):
        runner = unprocessed.main
    else:
        from prices import run as prices_run
        runner = prices_run.main
    argv = ["--supplier", profile.key] + ([] if dry else ["--apply"])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = runner(argv)
    except SystemExit as exc:
        return buf.getvalue() + f"\nОТМЕНА: {exc}", 2
    except Exception as exc:
        return buf.getvalue() + f"\nСБОЙ: {type(exc).__name__}: {exc}", 3
    return buf.getvalue(), code or 0


def pending_count(key):
    """Сколько строк новинок ждут человека НА ВКЛАДКЕ. None — не смогли.

    Считаем тем же правилом, что и вкладка (`catalog.pending_in_stock`): только позиции,
    которые есть у поставщика в свежем прайсе. Вся очередь `pending` для сводки не годится —
    в ней висит хвост позиций, выбывших из наличия, и сообщение звало разбирать работу,
    которой на вкладке нет (Сакура 13.08.2026: 411 в очереди против 1 на вкладке).
    """
    try:
        from prices import catalog
        return catalog.pending_in_stock(key)
    except Exception:
        return None


def troubles(out):
    """Только ошибки и предупреждения прогона — консольную простыню в бот не тащим."""
    found = []
    for ln in out.splitlines():
        s = ln.strip()
        if s.startswith("⚠"):
            found.append(s[:300])
        elif s.startswith(("ОШИБКА:", "ОТМЕНА:", "СБОЙ:")):
            found.append("❗ " + s[:600])
        elif s.startswith("аномалии цены:"):
            m = re.match(r"аномалии цены: (\d+) \(снято с загрузки (\d+)\)", s)
            if m and m.group(1) != "0":
                found.append(f"⚠ аномалии цены: {m.group(1)}, снято с загрузки {m.group(2)}")
    return found


def message(profile, out, code, dry, after):
    """Одна строка: поставщик и сколько новинок ждёт человека. -> текст или None (молчать).

    Чем кончился прогон, что было в файле, какие документы завелись — есть в логе и на
    дашборде; в телефоне нужен один итог (решение Сергея 27.08.2026). Единственное, о чём
    молчать нельзя, — НЕУДАЧНЫЙ прогон: без вердикта сломанная загрузка выглядит ровно как
    «новинок нет». Поэтому на ненулевом коде к строке возвращаются вердикт и ошибки.

    Удачный прогон с НУЛЁМ новинок молчит вовсе (решение Сергея 28.08.2026): работы человеку
    он не даёт, а письмо несопоставленного приходит по пять раз в сутки — из полезного сигнала
    получался фон, в котором тонет строка с настоящей новинкой. Молчание тут не «потеря
    события»: что прайс не пришёл — скажет сторож просрочки, что прогон сломался — скажет
    вердикт ниже, а сам факт загрузки виден в логе и на дашборде.
    """
    if code == 0 and after == 0:
        return None
    title = f"{profile.title} — Новинок — {after if after is not None else '?'}"
    if dry:
        title += " (сухой прогон)"
    if code == 0:
        return title
    verdict = {2: "ОТМЕНА", 3: "СБОЙ"}.get(code, "ОШИБКА")
    return "\n".join([f"{title} — {verdict}"] + troubles(out))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сторож почты: новый прайс -> оприходование")
    ap.add_argument("--supplier", default="kaktus_msk")
    ap.add_argument("--dry", action="store_true", help="прогон без записи в МойСклад")
    ap.add_argument("--force", action="store_true", help="грузить, даже если письмо не новое")
    ap.add_argument("--quiet", action="store_true", help="не слать телеграм")
    ap.add_argument("--check-overdue", action="store_true",
                    help="только посчитать просрочку и напечатать — ни почты, ни телеграма")
    args = ap.parse_args(argv)

    profile = profile_of(args.supplier)
    raw = getattr(profile, "unprocessed", False)
    logfile = LOG_DIR / f"prc_price_watch_{profile.key}.log"

    if args.check_overdue:
        # Проверка читает только историю приходов, в почту не ходит — годится и как ручная
        # сверка, и как способ увидеть текст, ничего не отправив.
        print(overdue_alert(profile, logfile, check_only=True) or "просрочки нет")
        return 0

    # Замок на сутки ставим ДО почты: незачем открывать IMAP-сессию каждые полчаса до вечера,
    # когда сегодняшний прайс уже оприходован.
    # Замок на сутки — про поставщика, который присылает прайс раз в день. Письмо
    # несопоставленного приходит НЕСКОЛЬКО раз в день и каждый раз другим составом
    # (внешний загрузчик разбирает остаток), поэтому замка там нет: работает только
    # проверка «письмо новее уже обработанного».
    state = read_state(profile.key)
    if done_today(state) and not args.force and not raw:
        log(logfile, f"сегодняшний прайс уже загружен ({state.get('filename')}, "
                     f"{state.get('date')}) — до завтра в почту не хожу")
        return 0

    fails = LOG_DIR / f"prc_price_watch_{profile.key}_mailfail.txt"
    try:
        kw = {"extensions": profile.extensions} if getattr(profile, "extensions", None) else {}
        letter = fetch_latest_price(profile.mail_folder, pattern=profile.file_pattern, **kw)
    except Exception as exc:
        # Разовый таймаут — молча, в лог. Но если почты нет несколько проверок подряд, прайс
        # мог прийти и остаться незамеченным — об этом человеку надо сказать.
        n = (int(fails.read_text()) if fails.exists() else 0) + 1
        fails.parent.mkdir(parents=True, exist_ok=True)
        fails.write_text(str(n))
        log(logfile, f"ПОЧТА НЕДОСТУПНА ({n} подряд): {type(exc).__name__}: {exc}")
        if n == MAIL_ALERT_AFTER and not args.quiet:
            log(logfile, "телеграм: " + tg(
                f"❌ {profile.title}: почта недоступна {n} проверки подряд — прайс может "
                f"пройти мимо.\n{type(exc).__name__}: {exc}"))
        return 3
    fails.unlink(missing_ok=True)
    if not letter:
        log(logfile, f"в папке «{profile.mail_folder}» писем с прайсом нет")
        return 0

    note_arrival(profile, letter)
    # Просрочку проверяем ДО выхода по «письмо не новое»: у молчащего поставщика последнее
    # письмо как раз старое, и после того выхода эта ветка была бы недостижима — то есть
    # сторож молчал бы ровно в том случае, ради которого он и заведён.
    overdue_alert(profile, logfile, quiet=args.quiet)

    if not (args.force or is_new(letter, state)):
        log(logfile, f"письмо не новое ({letter['filename']}, {letter['date']}) — пропуск")
        return 0

    log(logfile, f"новое письмо: {letter['filename']} / {letter['date']} — запускаю загрузку"
                 + (" (сухой прогон)" if args.dry else ""))
    out, code = run_load(profile, args.dry)
    after = pending_count(profile.key)
    log(logfile, f"итог {code}\n" + out)
    # Состояние пишем при ЛЮБОМ исходе: отменённую по аномалиям загрузку человек разбирает
    # руками, а сторож не должен долбить его тем же письмом каждые полчаса.
    if not args.dry:
        write_state(profile.key, letter, code)
    text = message(profile, out, code, args.dry, after)
    if not args.quiet and text:
        log(logfile, "телеграм: " + tg(text))
    elif not text:
        log(logfile, "телеграм: молчу — прогон удачный, новинок 0")
    return code


if __name__ == "__main__":
    sys.exit(main())

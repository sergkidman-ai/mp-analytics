# поток: prc — ежедневная сводка «запуск новинок на МП» в телеграм
"""Наталье в ds_prc_bot: что завели вчера и что застряло по дороге на витрины.

Зачем сводка, если есть вкладка «🚀 Запуск на МП». Вкладка отвечает на вопрос «как у нас
дела», а сводка — на вопрос «есть ли что делать сегодня»: пока карточку не завели, отчёт
никто не открывает, потому что повода нет. Поэтому письмо приходит само и молчит, когда
и правда всё в порядке (нечего заводить и никто не застрял).

Застрявшая модель — заведена больше недели назад, а хотя бы одной из пяти витрин до сих
пор нет. Порог в семь дней выбрал Сергей: карточка, поставленная в тот же день, до продажи
доходит за пару суток, и всё, что живёт неделю без витрины, — это забытая работа, а не
модерация. Список ограничен десятью строками: сводка — напоминание, разбираться идут
во вкладку, ссылка на неё в конце.

Адресат — своя переменная `TG_PRC_LAUNCH_ID`, а НЕ общая `TG_PRC_NOVELTY_ID`: там оператор
разбора прайсов, которому эта сводка не нужна. Нет переменной — сводка не шлётся и говорит
об этом в лог, чтобы молчание не выглядело как «всё хорошо».

Запуск: `./venv/bin/python -m ops.prc_launch_digest`   `--dry` — показать текст, не отправляя
"""
import argparse
import datetime
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mp-analytics")
load_dotenv("/opt/mp-analytics/.env")

from core import db  # noqa: E402
from collectors.launch_track import SHOWCASES  # noqa: E402

TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "")
CHAT = os.getenv("TG_PRC_LAUNCH_ID", "")
PAGE = "https://bi.metaverseworld.ru/warehouse/novelties"
STUCK_DAYS = 7
TITLES = dict(SHOWCASES)


def build():
    """Текст сводки. Возвращает None, когда говорить не о чем."""
    today = datetime.date.today()
    fresh = db.query("""SELECT external_code, ms_name FROM prc_launch
                         WHERE ms_created >= %s AND NOT archived ORDER BY external_code""",
                     (today - datetime.timedelta(days=1),))
    stuck = db.query("""
        SELECT l.external_code, l.ms_name, l.ms_created,
               string_agg(p.showcase, ',' ORDER BY p.showcase) FILTER (WHERE p.state = 'none') miss
          FROM prc_launch l JOIN prc_launch_mp p USING (external_code)
         WHERE l.ms_created IS NOT NULL AND l.ms_created <= %s AND NOT l.archived
         GROUP BY 1, 2, 3 HAVING count(*) FILTER (WHERE p.state = 'none') > 0
         ORDER BY l.ms_created, l.external_code""",
        (today - datetime.timedelta(days=STUCK_DAYS),))
    if not fresh and not stuck:
        return None

    lines = [f"🚀 Запуск новинок на МП — {today:%d.%m}",
             "Считаем только новые внешние коды: привязка товара поставщика",
             "к действующему коду новинкой не считается."]
    if fresh:
        lines.append(f"\nЗаведено за сутки: {len(fresh)} моделей")
        for r in fresh[:10]:
            lines.append(f"  {r['external_code']} — {(r['ms_name'] or '')[:60]}")
    else:
        lines.append("\nЗа сутки новых моделей нет.")

    if stuck:
        # Поимённый список из полутора сотен строк Наталья читать не станет, и через неделю
        # он придёт таким же. Поэтому сначала счёт по витринам — он показывает системную
        # дыру (витрину, на которую новинки просто не заводят), а поимённо только свежие:
        # модель месячной давности доделывают планово, вчерашнюю — сегодня.
        per = {}
        for r in stuck:
            for k in (r["miss"] or "").split(","):
                per[k] = per.get(k, 0) + 1
        lines.append(f"\nНовинки прошлых месяцев (заведены дольше {STUCK_DAYS} дней назад,\nа витрины нет): {len(stuck)} моделей")
        for k, n in sorted(per.items(), key=lambda x: -x[1]):
            lines.append(f"  нет на витрине {TITLES.get(k, k)}: {n}")
        recent = sorted(stuck, key=lambda r: r["ms_created"], reverse=True)[:10]
        lines.append("\nСамые свежие из них:")
        for r in recent:
            where = ", ".join(TITLES.get(k, k) for k in (r["miss"] or "").split(","))
            lines.append(f"  {r['external_code']} ({(today - r['ms_created']).days} дн) — нет: {where}")
    else:
        lines.append("\nНезакрытых новинок прошлых месяцев нет — все на витринах.")

    lines.append(f"\nПодробно: {PAGE} → «🚀 Запуск на МП»")
    return "\n".join(lines)


def main(dry=False):
    text = build()
    if text is None:
        print("сводка не нужна: за сутки пусто и застрявших нет")
        return
    if dry:
        print(text)
        return
    if not TG_TOKEN or not CHAT:
        print("не отправлено: нет TG_PRC_BOT_TOKEN или TG_PRC_LAUNCH_ID в .env")
        return
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": text[:4000],
                            "disable_web_page_preview": "true"}, timeout=60)
    print("отправлено" if r.ok else f"ошибка {r.status_code}: {r.text[:120]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="показать текст, не отправляя")
    main(**vars(ap.parse_args()))

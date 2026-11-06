# поток: mkt
"""Scheduled existing plan importer/optimizer with a report to Natalia's prc bot."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
from pathlib import Path

from ops import wb_plan_raise as optimizer
from ops import wb_promo_plan_import as importer
from ops.wb_promo_ack import ACC_NAME, MSK, api, recipient
from core.wb_price_writer import legacy_main

LOCK = Path('/run/lock/mp-wb-plan-import.lock')
REPORTS = Path('/opt/mp-analytics/runtime/wb_plan_raise_reports')


@contextmanager
def plan_lock():
    with LOCK.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def message(report):
    title = '✅' if report['exit_code'] == 0 else '⚠️'
    lines = [f'{title} WB — повышение цен в акциях', report['started_msk'], '']
    for acc in ACC_NAME:
        stats = report.get('accounts', {}).get(acc)
        lines.append(ACC_NAME[acc])
        if not stats or 'selected' not in stats:
            lines.append('Расчёт не завершён / не запускался.')
            continue
        lines.append(f"Кандидатов: {stats['candidates']}; уже обработано сегодня: {stats['daily_skipped']}.")
        lines.append(f"Выбрано: {stats['selected']}; подтверждено: {stats['confirmed']}; "
                     f"не подтверждено: {stats['unconfirmed']}.")
        remaining = stats['selected'] - stats['confirmed'] - stats['unconfirmed']
        if remaining:
            lines.append(f'Не отправлено: {remaining}.')
        if stats['confirmed']:
            lines.append(f"Прибавка цены: {stats['gain']:,.0f} ₽ суммарно на 1 шт. каждой подтверждённой карточки.")
        lines.append('')
    if report.get('error'):
        lines.append(report['error'])
    elif report['exit_code']:
        lines.append('Есть неподтверждённые изменения или запуск отложен. Подробности в журнале.')
    lines.append('Потолок всех учитываемых акций, пол прибыли и шаг до 15% сохранены.')
    lines.append('Участие оценивается по ценовым условиям выгрузок; это не отчёт о продажах.')
    return '\n'.join(lines)


def execute(apply=False):
    report = dict(started_msk=datetime.now(MSK).strftime('%d.%m.%Y %H:%M МСК'),
                  accounts={}, stage='импорт выгрузок', exit_code=1)
    try:
        with plan_lock():
            if apply:
                importer.auto()
            report['stage'] = 'запуск повышения'
            report['exit_code'] = legacy_main(
                lambda: optimizer.main(report, argv=['--apply'] if apply else []))
    except BlockingIOError:
        report.update(exit_code=75, error='Запуск отложен: импорт или повышение цен уже выполняется.')
    except Exception as exc:
        detail = str(exc)
        # Known business errors contain campaign titles, never tokens or request URLs.
        if not detail.startswith(('Нужны выгрузки новых автоакций:', 'Нет подтверждённого полного набора')):
            detail = type(exc).__name__
        report['error'] = f"Ошибка на этапе «{report['stage']}»: {detail[:700]}"
    report['finished_msk'] = datetime.now(MSK).isoformat()
    if apply:
        REPORTS.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = REPORTS / (datetime.now(MSK).strftime('%Y%m%d_%H%M%S_%f') + '.json')
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        try:
            result = api('sendMessage', dict(chat_id=recipient(), text=message(report)))
            report['telegram_message_id'] = int(result['message_id'])
        except Exception as exc:
            report['notification_error'] = type(exc).__name__
            print('WB price report: Telegram delivery failed; report saved locally', flush=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(message(report), flush=True)
    return report['exit_code'] or (1 if report.get('notification_error') else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    return execute(apply=args.apply)


if __name__ == '__main__':
    raise SystemExit(main())

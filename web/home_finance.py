# поток: fin
"""Join restored home sources without changing accounting or collecting APIs."""
from copy import deepcopy
import datetime as dt


def complete_totals(result, org):
    """Never present a subtotal of available cabinets as the business total."""
    missing = result.get('missing_accounts') or []
    for platform in ('wb', 'ozon', 'yandex'):
        if any(m.startswith(platform + ':') for m in missing):
            result[platform] = None
    if missing:
        result['known_subtotal'] = deepcopy(result['total'])
        result['total'] = {k: None for k in result['total']}
    result['financials_complete'] = not missing and result['total'].get('net') is not None
    return result


def enrich(result, org, orgs, query, opex_total, unassigned, bank_money, mp_money):
    result = complete_totals(result, org)
    month = result['period'][:7] + '-01'
    coverage = query("""SELECT org_inn,bank,min(operation_date)::text first_day,
        max(operation_date)::text last_day FROM bank_txn
        WHERE (%s='' OR org_inn=%s) GROUP BY 1,2 ORDER BY 1,2""", (org, org))
    result['statement_coverage'] = coverage
    selected = [org] if org else orgs
    # A saved expense month proves a known zero too. Absence is not a zero.
    closed = query("""SELECT org_inn FROM opex_month_closure
        WHERE month=%s AND reopened_at IS NULL AND (%s='' OR org_inn=%s)""", (month, org, org))
    known = {r['org_inn'] for r in closed}
    known.update(r['org_inn'] for r in coverage if r['first_day'][:7] <= month[:7] <= r['last_day'][:7])
    ready = all(o in known for o in selected)
    result['opex'] = opex_total(month, org) if ready else None
    result['opex_org_split'] = True
    counts = [unassigned(month, o) for o in selected] if ready else []
    result['opex_unassigned'] = {'n': sum(r['n'] for r in counts), 's': round(sum(r['s'] for r in counts), 2)}
    net = result['total'].get('net')
    expense = result['opex']
    result['net_after_opex'] = round(net-expense, 2) if net is not None and expense is not None else None
    result['opex_closed'] = all(o in {r['org_inn'] for r in closed} for o in selected)
    result['bank_money'] = bank_money(org)
    result['mp_money'] = mp_money(org)
    # The procurement cache has not been restored. Do not convert an empty sum
    # into zero or attribute a business-wide purchase total to one legal entity.
    result['purchases'] = {'net': None, 'docs': None}
    gaps = ['Закупки: месячная витрина приёмок и возвратов поставщикам не восстановлена.',
            'Недельная прибыль: детализация ещё не прошла финансовую проверку; месячные итоги её не заменяют.']
    if result.get('missing_accounts'):
        gaps.append('Нет отчётов кабинетов: ' + ', '.join(result['missing_accounts']) + '. Общие итоги не рассчитаны.')
    if net is None:
        gaps.append('Прибыль и себестоимость МП за выбранный месяц не подтверждены.')
    if not ready:
        gaps.append('Нет подтверждённых операционных расходов за выбранный месяц для всех выбранных организаций.')
    if result['opex_unassigned']['n']:
        gaps.append('Есть платежи без статьи: прибыль после распределённых расходов предварительная.')
    bank = result['bank_money'] or {}
    if any(a.get('as_of', '')[:7] < dt.date.today().isoformat()[:7] for a in bank.get('accounts', [])):
        gaps.append('Часть банковских остатков относится к прошлому месяцу: свежей выписки по этим счетам нет.')
    if bank.get('missing'):
        gaps.append('Банковский остаток неполный: есть счета без опорного остатка.')
    from reports import archived_finance
    current = dt.date.fromisoformat(month)
    previous = (current-dt.timedelta(days=1)).replace(day=1).isoformat()
    prior = archived_finance.business(previous, org)
    result['prev_period'] = previous if prior else None
    result['delta'] = None
    if prior:
        prior = complete_totals(prior, org)
        result['delta'] = {}
        for key, field in [('total_revenue','revenue'),('total_net','net'),('total_margin','margin_pct')]:
            now, old = result['total'].get(field), prior['total'].get(field)
            result['delta'][key] = None if now is None or old is None else {
                'abs': round(now-old,2), 'pct': round((now-old)/abs(old)*100,1) if old else None}
    result['data_gaps'] = gaps
    result['recovery_note'] = ('Месячные показатели — из Отчётов МП; операционные расходы — из Выписок и разнесения. '
        'Остатки денег показаны на последние известные даты, независимо от выбранного месяца. '
        + ('Исторические отчёты сохранены с точностью до рубля. ' if result['data_source']=='saved_monthly_reports' else '')
        + ('Есть предварительные показатели МП. ' if result.get('provisional') else '')
        + ' '.join(gaps))
    return result

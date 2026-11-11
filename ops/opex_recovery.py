# поток: inv
"""Restore documented expense rules through the existing OPEX engine.

No bank/MS mutations. Preview by default; --apply inserts absent rules only and
classifies unassigned payments. Existing manual assignments/rules stay intact.
"""
import argparse
import json
import re
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db
from collectors import bank_txn_store

CATEGORIES = {'Налоги ФОТ': 25, 'НДС': 30, 'УСН': 31, 'Программист': 50,
              'Бухгалтерия': 51, 'Упаковка': 60, 'Реклама и продвижение': 70, 'Доставка': 80}


def propose(rows, suppliers):
    out = []
    def add(inn, category, basis, purpose=None, lo=None, hi=None, name=None):
        out.append(dict(cp_inn=inn or None, cp_name_key=name, purpose_like=purpose,
                        amount_min=lo, amount_max=hi, category=category,
                        spread_months=1, spread_back=False, basis=basis))
    for inn in sorted(suppliers):
        if inn and inn not in {'7807355364', '7811803918'}:
            add(inn, 'Закупка товара у поставщиков', 'BRIEF_INV:1739–1773: supplier registry')
    for inn in ['7807355364', '7811803918']:
        add(inn, 'Перевод между своими счетами', 'BRIEF_INV:1759: own organizations')
    add('7727406020', 'Налоги ФОТ', 'BRIEF_INV:1454–1461: approved 40000 boundary', hi=40000)
    add('7727406020', 'Налоги и взносы', 'BRIEF_INV:1454–1461: approved 40000 boundary', lo=40000)
    # Reconstruct narrow purpose rules only from explicit text; no guessed payee roles.
    for row in rows:
        if row['direction'] != 'DEBIT':
            continue
        purpose = row.get('purpose') or ''
        inn = row.get('cp_inn') or None
        name = None if inn else bank_txn_store.name_key(row.get('cp_name'))
        if not inn and not name:
            continue
        bank_fee = re.search(r'ком-я|комисси|ком\.\s*за|ком-?я\s*за|плата за.*(?:обслужив|перевод|пакет)', purpose, re.I)
        pairs = [('Банковское обслуживание', r'ком-я|комисси|ком\.\s*за|плата за.*(?:обслужив|перевод|пакет)')]
        if not bank_fee:
            pairs += [
                ('Налоги ФОТ', r'ндфл|страховые взносы|страховых взносов|стр\. взносы|взносы на травматизм'),
                ('ФОТ', r'заработной платы|заработная плата|заработную плату|зарплат\w*'),
                ('Самозанятые', r'самозанят\w*|налог на профессиональный доход|режим НПД'),
                ('Программист', r'услуги программирования|разработка программного обеспечения|сопровождение программного обеспечения|работ по автоматизации процессов'),
                ('Аренда', r'аренд\w*'),
                ('Бухгалтерия', r'бухгалтерских и юридических услуг|бухгалтерские услуги'),
                ('Интернет и связь', r'хостингового сч[её]та'),
            ]
            if re.search(r'казнач|уфк|фнс', row.get('cp_name') or '', re.I) or re.search(r'ЕНП|налогов[а-яё\s]+сч[её]т', purpose, re.I):
                pairs += [('УСН', r'УСН|упрощенн\w*'), ('НДС', r'уплата НДС|уплате НДС|налог на добавленную стоимость|\(НДС\)')]
        for category, pattern in pairs:
            hit = re.search(pattern, purpose, re.I)
            if hit:
                add(inn, category, 'explicit payment purpose; reconstruction, not a recovered historical rule', hit.group(), name=name)
                break
        if re.search('альфа', row.get('cp_name') or '', re.I) and 'расчеты через ту' in purpose.lower():
            add(inn, 'Бухгалтерия', 'BRIEF_INV:1467: historical user rule', 'Расчеты через ТУ', name=name)
        if inn == '7707083893' and re.search('cargo-avto', purpose, re.I):
            add(inn, 'Упаковка', 'BRIEF_INV:1409–1412: narrow merchant rule', 'cargo-avto')
    salary_parties = set()
    for row in rows:
        if row['direction']=='DEBIT' and re.search(r'заработн.*плат|зарплат', row.get('purpose') or '', re.I):
            inn = row.get('cp_inn') or None
            if inn and len(inn)==12:
                salary_parties.add((inn,None))
            elif not inn:
                salary_parties.add((None,bank_txn_store.name_key(row.get('cp_name'))))
    for row in rows:
        inn=row.get('cp_inn') or None
        key=(inn,None if inn else bank_txn_store.name_key(row.get('cp_name')))
        if row['direction']=='DEBIT' and key in salary_parties and re.search('аванс за',row.get('purpose') or '',re.I):
            add(inn,'ФОТ','explicit advance to an independently identified salary recipient','аванс за',name=key[1])
    unique = {}
    for row in out:
        key = (row['cp_inn'], row['cp_name_key'], (row['purpose_like'] or '').lower(), row['amount_min'], row['amount_max'])
        if key in unique and unique[key]['category'] != row['category']:
            raise ValueError('Conflicting recovery rules')
        unique[key] = row
    return list(unique.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--report', type=Path, required=True)
    args = ap.parse_args()
    rows = db.query('select * from bank_txn order by operation_date,id')
    suppliers = {r['inn'] for r in db.query('select distinct inn from supplier_payment_terms') if r['inn']}
    rules = propose(rows, suppliers)
    report = {'proposed': len(rules), 'rules': rules, 'historical_backup_found': False}
    if args.apply:
        with db.get_conn() as conn, conn.cursor() as cur:
            cur.execute('select pg_advisory_xact_lock(21901110)')
            for name, sort in CATEGORIES.items():
                cur.execute('insert into opex_category(name,sort) values(%s,%s) on conflict(name) do nothing', (name,sort))
            cur.execute('select id,name from opex_category')
            ids = {name: id for id,name in cur.fetchall()}
            inserted = 0
            for r in rules:
                cur.execute('''insert into opex_rule(cp_inn,cp_name_key,purpose_like,amount_min,amount_max,category_id,spread_months,spread_back)
                    values(%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing''',
                    (r['cp_inn'],r['cp_name_key'],r['purpose_like'],r['amount_min'],r['amount_max'],ids[r['category']],1,False))
                inserted += cur.rowcount
        report['inserted'] = inserted
        report['assigned'] = bank_txn_store.apply_rules()
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print({k:v for k,v in report.items() if k != 'rules'})


if __name__ == '__main__':
    main()

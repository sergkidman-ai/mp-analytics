# поток: cf
"""Union bank and conducted MS payments without counting matched documents twice.

Expense classification uses the SAME opex_rule table and specificity ordering as
statements, with bank manual assignments taking precedence. No writes here.
"""
import datetime as dt
from decimal import Decimal
import re
import uuid
from core import db
from collectors.bank_txn_store import name_key


def expense_rules():
    return db.query('''select r.*, c.name category, w.n_match
        from opex_rule r join opex_category c on c.id=r.category_id
        join opex_rule_reach w on w.rule_id=r.id
        order by w.n_match, length(coalesce(r.purpose_like,'')) desc, r.id desc''')


def expense_category(row, rules):
    for rule in rules:
        if row.get('cp_inn'):
            if rule.get('cp_inn') != row['cp_inn']:
                continue
        elif not rule.get('cp_name_key') or rule['cp_name_key'] != name_key(row.get('cp_name')):
            continue
        if (rule.get('purpose_like') or '').lower() not in (row.get('purpose') or '').lower():
            continue
        amount = Decimal(str(row['amount']))
        if rule.get('amount_min') is not None and amount < rule['amount_min']:
            continue
        if rule.get('amount_max') is not None and amount >= rule['amount_max']:
            continue
        return rule['category']
    return None


def normalize_ms(row):
    d = row['payload']
    if d.get('applicable') is not True:
        return None
    agent = d.get('agent') or {}
    return dict(id=d['id'],org_inn=row['org_inn'],direction='CREDIT' if row['entity']=='paymentin' else 'DEBIT',
                amount=Decimal(str(d['sum']))/100, day=dt.date.fromisoformat(d['moment'][:10]),
                cp_inn=agent.get('inn'),cp_name=agent.get('name'),purpose=d.get('paymentPurpose') or '',
                sync=d.get('syncId'),ms_name=d.get('name'),numbers={str(n).strip() for n in [d.get('name'),d.get('incomingNumber')] if n is not None},
                source='ms:'+row['entity'],ref=d['id'],opex_cat=None)


def _purpose(value):
    return re.sub(r'\s+', '', value or '').lower()


def merge(bank, ms):
    """One-to-one matching; UUID first, otherwise exact date/payee/purpose or doc number.

    Bank settlement date/manual category remain authoritative for a matched pair.
    An amount alone is never a payment identity.
    """
    bank = [dict(b, source='bank:'+b['bank'],ref=str(b['id'])) for b in bank]
    used = set()
    unmatched = []
    for m in ms:
        cand = [b for b in bank if b['id'] not in used and b['org_inn']==m['org_inn'] and
                b['direction']==m['direction'] and Decimal(str(b['amount']))==m['amount']]
        uid = m.get('sync')
        exact = [b for b in cand if uid and (uid==b.get('txn_uuid') or
                 (b.get('transaction_id') and uid==str(uuid.uuid5(uuid.NAMESPACE_DNS,b['transaction_id']))))]
        if not exact:
            exact = []
            for b in cand:
                if b['day'] != m['day']:
                    continue
                same_party = (b.get('cp_inn') and b['cp_inn']==m.get('cp_inn')) or (
                    not b.get('cp_inn') and not m.get('cp_inn') and name_key(b.get('cp_name')) and
                    name_key(b.get('cp_name'))==name_key(m.get('cp_name')))
                same_doc = b.get('document_number') and str(b['document_number']).strip() in m['numbers']
                same_purpose = _purpose(b.get('purpose')) and _purpose(b.get('purpose'))==_purpose(m.get('purpose'))
                if same_party and (same_doc or same_purpose):
                    exact.append(b)
        if len(exact)==1:
            used.add(exact[0]['id'])
            exact[0]['ms_ref']=m['ref']
            exact[0]['ms_name']=m.get('ms_name')
        else:
            unmatched.append(m)
    return bank+unmatched, {'bank':len(bank),'ms':len(ms),'matched':len(used),'ms_extra':len(unmatched)}


def facts(date_from,date_to):
    from cf.build import ORGS,OPEX_TO_CF,TRANSFER,income_rules,classify_income,tax_category
    bank=db.query('''select t.*,t.operation_date::date as day,
        case when h.id is null then c.name else s.category end as opex_cat,h.id is not null as opex_closed
        from bank_txn t left join bank_txn_opex o on o.txn_id=t.id
        left join opex_category c on c.id=o.category_id
        left join opex_month_closure h on h.org_inn=t.org_inn and h.month=date_trunc('month',t.operation_date)::date and h.reopened_at is null
        left join opex_closed_txn s on s.close_id=h.id and s.txn_id=t.id
        where t.operation_date between %s and %s and t.org_inn=any(%s) and (h.id is null or s.txn_id is not null) order by t.id''',
        (date_from,date_to,list(ORGS)))
    ms=[m for row in db.query('select * from cf_ms_payment where day between %s and %s order by entity,id',
                              (date_from,date_to)) if (m:=normalize_ms(row)) is not None]
    rows,stats=merge(bank,ms)
    inc,exp=income_rules(),expense_rules()
    out=[]
    for row in rows:
        if row.get('cp_inn') == row['org_inn']:
            continue
        if row['direction']=='CREDIT':
            cat=classify_income(row,inc)
            amount=row['amount']
        else:
            article=row.get('opex_cat') if row.get('opex_closed') else row.get('opex_cat') or expense_category(row,exp)
            cat=OPEX_TO_CF.get(article or '', 'other_out')
            if cat=='taxes':
                cat=tax_category(row.get('purpose'))
            # Transfers to the OTHER legal entity are real cash flows for each entity.
            if cat==TRANSFER:
                cat='other_out'
            amount=-row['amount']
        note=f"{row.get('cp_name') or ''} · {row.get('purpose') or ''}"[:250]
        if row.get('ms_ref'):
            note=f"Сверено с МС №{row.get('ms_name') or '—'} · "+note
        if row['source'].startswith('ms:'):
            note=f"МС №{row.get('ms_name') or '—'} · "+note
        if row.get('cp_inn') in set(ORGS)-{row['org_inn']}:
            note='между фирмами · '+note
        out.append(dict(org_inn=row['org_inn'],day=row['day'],kind='fact',category=cat,
                        amount=float(amount),source=row['source'],ref=row['ref'],note=note,estimate=False))
    return out

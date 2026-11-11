# поток: cf
import datetime as dt
from decimal import Decimal
from cf.payment_facts import merge,normalize_ms,expense_category
from ops.opex_recovery import propose

D=dt.date(2026,10,1)


def bank(**kw):
    return dict(dict(id=1,org_inn='7807355364',direction='DEBIT',amount=Decimal('100'),
        day=D,bank='alfa',cp_inn='supplier',cp_name='Supplier',purpose='Payment for goods',
        document_number='1',txn_uuid='sync',transaction_id=None,opex_cat='Программист'),**kw)


def ms(**kw):
    return dict(dict(id='ms1',org_inn='7807355364',direction='DEBIT',amount=Decimal('100'),
        day=D,cp_inn='supplier',cp_name='Supplier',purpose='Payment for goods',
        numbers={'different-ms-name'},sync='sync',source='ms:paymentout',ref='ms1'),**kw)


def test_matched_payment_counted_once_and_manual_category_survives():
    rows,stats=merge([bank()],[ms()])
    assert len(rows)==1 and stats['matched']==1
    assert rows[0]['opex_cat']=='Программист'
    assert rows[0]['ms_ref']=='ms1'


def test_same_amount_other_org_does_not_match():
    rows,stats=merge([bank()],[ms(org_inn='7811803918')])
    assert len(rows)==2 and stats['matched']==0


def test_same_amount_other_payee_without_uuid_does_not_match():
    rows,stats=merge([bank()],[ms(cp_inn='other',sync=None)])
    assert len(rows)==2 and stats['matched']==0


def test_different_bank_number_matches_identical_full_purpose():
    rows,stats=merge([bank()],[ms(sync=None)])
    assert len(rows)==1 and stats['matched']==1


def test_one_to_one_matching_does_not_collapse_two_payments():
    rows,stats=merge([bank()],[ms(),ms(id='ms2',ref='ms2',sync=None)])
    assert len(rows)==2 and stats['matched']==1


def test_unconducted_ms_payment_is_excluded():
    assert normalize_ms({'payload':{'applicable':False}}) is None


def test_ms_kopecks_are_exact_rubles():
    x=normalize_ms({'entity':'paymentout','org_inn':'7807355364','payload':dict(
        id='x',applicable=True,sum=12345,moment='2026-10-01 00:00:00',agent={})})
    assert x['amount']==Decimal('123.45')


def test_documented_threshold_boundary_and_no_guessed_programmer():
    rules=propose([],set())
    lo=next(r for r in rules if r['category']=='Налоги ФОТ')
    hi=next(r for r in rules if r['category']=='Налоги и взносы')
    assert lo['amount_max']==hi['amount_min']==40000
    assert not any(r['category']=='Программист' for r in rules)


def test_bank_commission_mentioning_self_employed_is_not_salary():
    row=bank(purpose='Ком-я за внеш.перевод на сч.ФЛ, применяющих режим НПД')
    rules=[r for r in propose([row],set()) if r['cp_inn']=='supplier']
    assert [r['category'] for r in rules]==['Банковское обслуживание']


def test_invoice_vat_is_not_a_tax_payment():
    row=bank(purpose='Оплата по счету 10. В т.ч. НДС 22% 3000 руб.')
    assert not any(r['category'] in ['НДС','УСН','Налоги ФОТ'] for r in propose([row],set()) if r['cp_inn']=='supplier')


def test_shared_rules_obey_amount_and_purpose():
    rule={'cp_inn':'supplier','purpose_like':'УСН','category':'УСН','amount_min':Decimal('40000'),'amount_max':None}
    assert expense_category(dict(bank(),purpose='Уплата УСН',amount=Decimal('40000')),[rule])=='УСН'
    assert expense_category(dict(bank(),purpose='Уплата УСН',amount=Decimal('39999')),[rule]) is None


def test_incomplete_ms_snapshot_never_replaces_existing_data(monkeypatch,tmp_path):
    import pytest
    from cf import ms_payments as source
    monkeypatch.setattr(source,'BASE',tmp_path)
    def get(path, params):
        if path=='/entity/organization':
            return {'rows':[{'inn':i,'meta':{'href':'org/'+i}} for i in sorted(source.ORGS)]}
        if path=='/entity/currency':
            return {'rows':[{'code':'643','meta':{'href':'RUB'}}]}
        return {'meta':{'size':21},'rows':[dict(id='1',moment='2026-10-01 00:00:00',
            organization={'meta':{'href':'org/7807355364'}},rate={'currency':{'meta':{'href':'RUB'}}},sum=100,applicable=True)]}
    monkeypatch.setattr(source.ms_api,'get',get)
    monkeypatch.setattr(source.db,'get_conn',lambda:pytest.fail('partial source published'))
    with pytest.raises(ValueError,match='Incomplete payment pagination'):
        source.collect(dt.date(2026,10,1),dt.date(2026,10,2))


def test_explicit_vat_and_programmer_purposes_are_recovered():
    tax=bank(cp_inn='tax',cp_name='Казначейство',purpose='Пополнение единого налогового счета (НДС) Без НДС.')
    program=bank(purpose='Оплата за выполнение работ по автоматизации процессов по договору ПБ-1')
    rules=propose([tax,program],set())
    assert any(r['cp_inn']=='tax' and r['category']=='НДС' for r in rules)
    assert any(r['cp_inn']=='supplier' and r['category']=='Программист' for r in rules)

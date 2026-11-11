# поток: fin
from collections import Counter
from decimal import Decimal
from pathlib import Path
import pytest
from ops.wb_loss_official import Catalogue
from ops.wb_loss_restore import archived_rows,expand_card


def test_archived_loss_rows_keep_accounts_leading_zeroes_and_cost_precision():
    rows=archived_rows((Path(__file__).parents[1]/'docs/reports/wb_lost_warehouses_2026-08-21.md').read_text())
    assert sum(r['qty'] for r in rows)==641
    assert {r['account'] for r in rows}=={'wb_acc1','wb_acc2'}
    assert all(r['warehouse'] and r['snapshot_date'] for r in rows)
    assert any(r['vendor_code'].startswith('0') for r in rows)
    assert all(r['archive']['precision']=='rounded_rubles' for r in rows)
    assert all(r['archive']['total_cost'] is None or r['archive']['total_cost']>0 for r in rows)


def test_whole_nested_bundle_and_universal_model_signature():
    c=Catalogue(['0001','0002','5335'],{'6452':['0001',{'external_code':'0002','quantity':2}]},[('0002','5335')])
    assert expand_card(c,'6452X3MS22I0')==Counter({'0001':3,'0002':6})
    assert c.signature(Counter({'0002':2}))==c.signature(Counter({'5335':2}))
    assert c.signature(Counter({'0002':2}))!=c.signature(Counter({'5335':1}))


def test_random_derivative_is_not_a_bundle_and_unknown_components_fail():
    c=Catalogue(['0064','4692'],{},[])
    assert expand_card(c,'0064X9775UHA')==Counter({'0064':1})
    assert expand_card(c,'4692X5BV7567','Картридж для принтера')==Counter({'4692':1})
    with pytest.raises(ValueError):expand_card(c,'4692X5BV7567')
    with pytest.raises(ValueError):expand_card(c,'9999')

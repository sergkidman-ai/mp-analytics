# поток: mkt
from unittest.mock import patch
from ops import wb_plan_raise as p
from ops import wb_promo_plan_import as imp


def calculate(plans, now=1000, cost=100, step=100):
    with patch.object(p.db, 'query', return_value=plans):
        return p.candidates('wb_acc1', goods={1: (2000, 50, now, '0010')},
                            costs={1: {'cogs': cost, 'bundle': 1}}, max_step=step)


def test_only_live_memberships_constrain_raise():
    rows = calculate([{'nm_id': 1, 'plan_price': 900},
                      {'nm_id': 1, 'plan_price': 1400},
                      {'nm_id': 1, 'plan_price': 1300}])
    assert len(rows) == 1
    assert rows[0]['cap'] == 1300
    assert rows[0]['target_price'] == 1300
    assert rows[0]['send_price'] == 2000


def test_floor_and_integer_discount_must_both_fit():
    assert calculate([{'nm_id': 1, 'plan_price': 1200}], cost=500) == []


def test_guard_does_not_tolerate_a_small_floor_breach():
    row = p.w.decide('wb_acc1', {1:(1000,36,640,'0001')},
                     {1:{'cogs':120.1,'bundle':1}}, {}, .5, touched_only=False)[0]
    assert row['status'] == 'todo'
    assert row['send_price']*(1-row['send_disc']/100) >= row['floor_price']


def test_step_limit_and_exact_discount_rounding():
    rows = calculate([{'nm_id': 1, 'plan_price': 1400}], step=.15)
    assert rows[0]['target_price'] <= 1150
    assert p.plan_to_discount(2000, 1400) == (2000, 30)
    assert p.plan_to_discount(2000, 2000) == (2000, 0)


def test_suffix_bundle_needs_confirmed_quantity_and_supply():
    from ops.promo_ordinary_scope import confirmed_wb_cost
    c = {'cogs': 100, 'branch': 'remote', 'bundle': 1}
    assert not confirmed_wb_cost('0001X6ABC', c, {'0001'}, set())
    c['bundle'] = 6
    assert confirmed_wb_cost('0001X6ABC', c, {'0001'}, set())
    c['branch'] = 'none'
    assert not confirmed_wb_cost('0001X6ABC', c, {'0001'}, set())


def test_expiry_must_be_present_in_query():
    with patch.object(p.db, 'query', return_value=[]) as q:
        assert p.candidates('wb_acc1', goods={}) == []
    assert "valid_to >=" in q.call_args.args[0]
    assert "Europe/Moscow" in q.call_args.args[0]


def test_importer_rejects_unverified_calendar_window(tmp_path):
    import pandas as pd
    frame = pd.DataFrame([{v: 'Да' if k == 'inp' else 'Цифровой квадрат' if k == 'brand'
                           else '0010' if k == 'vc' else 100 for k, v in imp.COLS.items()}])
    file = tmp_path / 'Все_товары_подходящие_для_акции_Неизвестная.xlsx'
    frame.to_excel(file, index=False)
    with patch('ops.wb_auto_entry.Client.get', return_value={'data': {'promotions': []}}), \
         patch.object(imp.db, 'upsert') as save:
        import pytest
        with pytest.raises(RuntimeError):
            imp.load(file, '2026-11-03', None)
        save.assert_not_called()
    assert imp.read_file(file)[2][imp.COLS['vc']].iloc[0] == '0010'


def test_new_auto_campaign_without_export_blocks_apply(tmp_path, monkeypatch):
    import json
    import pytest
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    start, end = (now-timedelta(days=1)).isoformat(), (now+timedelta(days=1)).isoformat()
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/wb_plan_raise_coverage.json').write_text(json.dumps({
        'generations': {'wb_acc1': [[1, start, end]]}}))
    monkeypatch.setattr(p, 'BASE_DIR', tmp_path)
    calendar = {2: {'id': 2, 'name': 'Новая автоакция', 'type': 'auto',
                    'startDateTime': start, 'endDateTime': end}}
    with patch('ops.wb_auto_entry.Client.calendar', return_value=calendar), \
         patch.object(p.db, 'query', return_value=[]):
        with pytest.raises(RuntimeError, match='Нужны выгрузки'):
            p.verify_calendar_coverage('wb_acc1')
    with patch('ops.wb_auto_entry.Client.calendar', return_value=calendar), \
         patch.object(p.db, 'query', return_value=[{'promo_key': 'Новая автоакция'}]):
        p.verify_calendar_coverage('wb_acc1')


def test_partial_run_saves_each_completed_batch_before_stopping(monkeypatch):
    from unittest.mock import Mock
    rows = [dict(nm_id=n, gain=10, cap=100, disc_before=40, send_disc=30,
                 status='dry', err=None) for n in range(3)]
    monkeypatch.setattr(p.sys, 'argv', ['wb_plan_raise', '--account', 'wb_acc1', '--apply'])
    monkeypatch.setattr(p, 'verify_calendar_coverage', lambda *args: None)
    monkeypatch.setattr(p, 'candidates', lambda *args: rows)
    monkeypatch.setattr(p.db, 'query', lambda *args: [])
    monkeypatch.setattr(p, 'BATCH', 2)
    monkeypatch.setattr(p.w, 'push', Mock(side_effect=[(True, ''), (False, 'pending')]))
    save = Mock()
    monkeypatch.setattr(p.w, 'save', save)
    assert p.main() == 1
    assert save.call_count == 2
    assert [r['status'] for r in save.call_args_list[0].args[0]] == ['confirmed', 'confirmed']
    assert save.call_args_list[1].args[0][0]['status'] == 'error'


def test_repeat_run_skips_confirmed_daily_step(monkeypatch):
    from unittest.mock import Mock
    rows = [dict(nm_id=n, gain=10, cap=100, disc_before=40, send_disc=30,
                 status='dry', err=None) for n in [1, 2]]
    monkeypatch.setattr(p.sys, 'argv', ['wb_plan_raise', '--account', 'wb_acc1', '--apply'])
    monkeypatch.setattr(p, 'verify_calendar_coverage', lambda *args: None)
    monkeypatch.setattr(p, 'candidates', lambda *args: rows)
    monkeypatch.setattr(p.db, 'query', lambda *args: [{'nm_id': 1}])
    upload = Mock(return_value=(True, ''))
    monkeypatch.setattr(p.w, 'push', upload)
    monkeypatch.setattr(p.w, 'save', Mock())
    assert p.main() == 0
    assert [r['nm_id'] for r in upload.call_args.args[1]] == [2]


def test_import_auto_does_not_replay_an_older_replaced_export(monkeypatch):
    from unittest.mock import Mock
    import datetime
    day = datetime.date.today().strftime('%Y%m%d')
    old, new = f'/drop/{day}_100000_акция.xlsx', f'/drop/{day}_110000_акция.xlsx'
    monkeypatch.setattr(imp.glob, 'glob', lambda *args, **kwargs: [old, new])
    monkeypatch.setattr(imp.db, 'query', lambda *args: [{'source_file': new.rsplit('/',1)[1]}])
    monkeypatch.setattr(imp, 'read_file', lambda *args, **kwargs: ('wb_acc1','Одна акция',None))
    monkeypatch.setattr(imp, 'load_regular', lambda *args: None)
    load = Mock()
    monkeypatch.setattr(imp, 'load', load)
    imp.auto()
    load.assert_not_called()

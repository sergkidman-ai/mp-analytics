# поток: fin
import json
from reports import legal_sales_page as page


def report(tmp_path,monkeypatch,months):
    row=dict(month='2026-10',date='2026-10-28',demand_id='test',demand_name='Test',
             order_name='Test',sale='100.00',paid='100.00',cogs='40.00',
             agent='Company',project='Digitalsquare',order_status='Доставлен')
    p=tmp_path/'report.json';p.write_text(json.dumps(dict(updated_at='2026-11-10',rows=[row],scanned_months=months)))
    monkeypatch.setattr(page,'SOURCE',p)
    return page.render()


def test_scanned_empty_month_is_visible_and_does_not_change_sales(tmp_path,monkeypatch):
    html=report(tmp_path,monkeypatch,['2026-10','2026-11'])
    assert '11.2026' in html and '10.2026' in html
    assert 'Месяц проверен. Продаж, соответствующих условиям отчёта, нет.' in html
    assert html.count('<details class="ls-month">')==2
    assert '<span>100,00 ₽</span>' in html


def test_unscanned_month_is_not_reported_as_zero(tmp_path,monkeypatch):
    html=report(tmp_path,monkeypatch,['2026-10'])
    assert '11.2026' not in html
    assert 'Месяц проверен.' not in html


def test_missing_source_is_still_shown_as_not_collected(tmp_path,monkeypatch):
    monkeypatch.setattr(page,'SOURCE',tmp_path/'missing.json')
    html=page.render()
    assert 'Данные ещё собираются.' in html
    assert 'Месяц проверен.' not in html

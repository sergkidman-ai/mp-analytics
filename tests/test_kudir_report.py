# поток: fin
from decimal import Decimal

import unittest

from reports import kudir_report as report


@unittest.skipUnless(report.source_path(1).exists(), "Local accountant source not attached")
def test_first_quarter_money_and_formula_ranges():
    data = report.load(1)
    assert len(data["operations"]) == 243
    assert data["calculated"] == {"income": "26052640.37", "expense": "24060620.29"}
    assert data["delta"] == {"income": "0.00", "expense": "0.00"}
    assert data["accountant"]["income_formula"] == "SUM(E7:E242)"
    assert data["income_rows_outside_formula"] == []
    assert max(o["row"] for o in data["operations"]) < data["accountant"]["row"]


@unittest.skipUnless(report.source_path(2).exists(), "Local accountant source not attached")
def test_second_quarter_detects_three_income_lines_omitted_by_excel():
    data = report.load(2)
    assert data["accountant"]["income"] == "27359773.35"
    assert data["calculated"] == {"income": "28647860.34", "expense": "24459992.38"}
    assert data["delta"] == {"income": "1288086.99", "expense": "0.00"}
    assert data["income_rows_outside_formula"] == [322, 323, 324]
    excluded = [o for o in data["operations"] if o["row"] in data["income_rows_outside_formula"]]
    assert sum(Decimal(o["income"]) for o in excluded) == Decimal(data["delta"]["income"])
    assert len(data["operations"]) == 324  # No quarter/cumulative/tax footer as postings.


@unittest.skipUnless(report.source_path(3).exists(), "Local accountant source not attached")
def test_third_quarter_matches_accountant_and_classifies_all_marketplace_costs():
    data = report.load(3)
    assert len(data["operations"]) == 283
    assert data["accountant"]["income_formula"] == "SUM(E7:E290)"
    assert data["accountant"]["expense_formula"] == "SUM(F7:F290)"
    assert data["calculated"] == {"income": "28106913.24", "expense": "22965282.97"}
    assert data["delta"] == {"income": "0.00", "expense": "0.00"}
    assert data["income_rows_outside_formula"] == []
    assert data["outside_document_dates"] == []
    groups = {group["category"]: group for group in data["groups"]}
    assert groups["Отчёты WB"]["income"] == "4859005.38"
    assert groups["Отчёты Маркет"]["expense"] == "2048591.68"
    assert groups["Себестоимость (месячные ручные строки)"]["expense"] == "10307272.16"


@unittest.skipUnless(report.source_path(2).exists(), "Local accountant source not attached")
def test_source_document_dates_are_not_silently_corrected():
    data = report.load(2)
    assert data["outside_document_dates"] == [243]
    op = next(o for o in data["operations"] if o["row"] == 243)
    assert op["document_date"].startswith("2028-")
    inherited = [o for o in data["operations"] if o["document_inherited"]]
    assert inherited
    assert all(o["document"] for o in inherited)


def test_excel_cached_float_is_rounded_to_kopecks():
    assert report.money(26052640.369999994) == Decimal("26052640.37")
    assert report.money(-58.455) == Decimal("-58.46")
    assert report.money(0) == Decimal("0.00")


def test_file_selector_accepts_only_registered_quarters():
    try:
        report.source_path("../../.env")
    except KeyError:
        return
    raise AssertionError("Unknown filename selector accepted")


@unittest.skipUnless(report.source_path(1).exists() and report.source_path(2).exists(), "Local accountant sources not attached")
def test_report_and_original_downloads():
    import hashlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.kudir_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for q, count in [(1, 243), (2, 324), (3, 283)]:
            page = client.get(f"/reports/kudir?quarter={q}")
            assert page.status_code == 200
            assert page.text.count("data-operation") == count + 1  # + selector in script
            assert 'КУДиР · Цифровой квадрат · 2026' in page.text
            assert 'class="rtab cur">📒 КУДиР' in page.text
            assert 'бухгалтер учтёт три доходные строки в годовом отчёте' in page.text if q == 2 else True
            data = client.get(f"/api/reports/kudir?quarter={q}").json()
            assert len(data['operations']) == count
            original = client.get(f"/reports/kudir/file/{q}")
            assert original.status_code == 200
            assert hashlib.sha256(original.content).hexdigest() == data['sha256']
            assert original.headers['cache-control'] == 'private, no-store'
        assert client.get('/reports/kudir/file/4').status_code == 404
        assert client.get('/reports/kudir?quarter=4').status_code == 404
        latest = client.get('/api/reports/kudir').json()
        assert latest['quarter'] == 3
        assert latest['tax']['current']['income'] == '82807413.95'
        assert 'Внешняя сверка III квартала не закрыта' in client.get('/reports/kudir').text
        assert client.get('/api/reports/kudir?quarter=0').status_code == 422


@unittest.skipUnless(report.source_path(1, 'diskver').exists(), "Local Diskver source not attached")
def test_diskver_quarter_sums_and_missing_excel_totals():
    expected = {
        1: (173, '6345359.51', '5163266.59'),
        2: (124, '11746206.19', '8275355.62'),
        3: (221, '4577644.80', '4300372.15'),
        4: (28, '126023.06', '372983.06'),
    }
    for q, (count, income, expense) in expected.items():
        data = report.load(q, 'diskver')
        assert data['org_inn'] == '7811803918'
        assert len(data['operations']) == count
        assert data['calculated'] == {'income': income, 'expense': expense}
        assert data['accountant']['income'] is None
        assert data['accountant']['expense'] is None
        assert data['delta'] == {'income': None, 'expense': None}
        assert data['accountant']['errors'] == {'income': '#NAME?', 'expense': '#NAME?'}
        assert data['is_partial'] == (q == 4)
        assert all(o['row'] < data['accountant']['row'] for o in data['operations'])


@unittest.skipUnless(report.source_path(1, 'diskver').exists(), "Local Diskver source not attached")
def test_diskver_routes_preserve_entity_and_full_workbook():
    import hashlib
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.kudir_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        default = client.get('/api/reports/kudir?org=diskver')
        assert default.status_code == 200
        assert default.json()['quarter'] == 3
        checksum = None
        for q in range(1, 5):
            data = client.get(f'/api/reports/kudir?org=diskver&quarter={q}').json()
            page = client.get(f'/reports/kudir?org=diskver&quarter={q}')
            assert page.status_code == 200
            assert 'КУДиР · Дисквер · 2026' in page.text
            assert '22 669 210,50 ₽' in page.text
            assert '17 738 994,36 ₽' in page.text
            assert 'Итоги сверены вручную' in page.text
            assert '#NAME?' not in page.text
            assert 'Нельзя сверить' not in page.text
            assert 'III квартал' in page.text and 'IV квартал' in page.text
            assert page.text.count('data-operation') == len(data['operations']) + 1
            if q == 4:
                assert 'IV квартал ещё не завершён' in page.text
                assert '13.10.2026' in page.text
            original = client.get(f'/reports/kudir/file/{q}?org=diskver')
            assert original.status_code == 200
            assert original.content[:2] == b'PK'
            assert hashlib.sha256(original.content).hexdigest() == data['sha256']
            assert original.headers['content-disposition'].endswith('"KUDIR_Diskver_2026.xlsx"')
            if checksum is not None:
                assert checksum == data['sha256']  # All quarter links download the original annual book.
            checksum = data['sha256']
        assert client.get('/reports/kudir?org=unknown').status_code == 422
        assert client.get('/reports/kudir/file/1?org=../../.env').status_code == 422
        assert client.get('/api/reports/kudir?org=diskver&quarter=5').status_code == 422


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn))

# поток: prc
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from prices import solutionsprint as run
from prices import solutionsprint_source as source


@pytest.mark.parametrize("name", [
    "SP", "Sprint", "Solution Print", "Solutions Print", "S.Print",
    "Standart", "STANDARD", "Солюшнс Принт",
])
def test_only_allowed_manufacturers(name):
    assert source.allowed_brand(name)


@pytest.mark.parametrize("name", [
    "Ricoh SP 150", "HP", "Cactus", "Sprinting", "",
])
def test_other_manufacturers_excluded(name):
    assert not source.allowed_brand(name)


def test_supplier_filters_and_readback():
    doc = page('', extra='''<label for="arrFilterCatalog_13_1">Solution Print</label>
      <label for="arrFilterCatalog_13_2">Standart</label><label for="arrFilterCatalog_13_3">HP</label>
      <label for="arrFilterCatalog_16_1">Совместимый</label>
      <label for="arrFilterCatalog_16_2">Оригинальный</label>''')
    params = source.selection_params(doc)
    assert params == {'arrFilterCatalog_13_1':'Y', 'arrFilterCatalog_13_2':'Y',
                      'arrFilterCatalog_16_1':'Y', 'set_filter':'Показать'}
    with pytest.raises(RuntimeError, match='not applied'):
        source.verify_selection(doc, params)
    checked = ''.join(f'<input name="{k}" checked>' for k in params if k != 'set_filter')
    source.verify_selection(page('', extra=checked), params)
    with pytest.raises(RuntimeError, match='not applied'):
        source.verify_selection(page('', extra=checked+'<input name="arrFilterCatalog_13_3" checked>'), params)


def test_no_allowed_manufacturer_is_empty_category():
    assert source.selection_params(page('', extra='<label for="arrFilterCatalog_13_1">HP</label>')) is None
    assert source.selection_params(page('')) == {}  # Sale needs per-product properties.


@pytest.mark.parametrize('brand,originality,expected', [
    ('Solution Print','Совместимый',True), ('Standart','Совместимый',True),
    ('Ricoh','Совместимый',False), ('Solution Print','Оригинальный',False)])
def test_product_properties_override_missing_brand_in_name(brand, originality, expected):
    doc = page('', extra=f'<div>Производитель принтера: HP</div><div>Производитель: {brand}</div>'
                         f'<div>Оригинальность: {originality}</div>')
    assert source.detail_scope(doc) is expected


def test_missing_product_properties_stops_import():
    with pytest.raises(RuntimeError, match='missing or ambiguous'):
        source.detail_scope(page(''))


def test_nested_product_property_labels():
    assert source.detail_scope(page('', extra='''
      <div><span>Производитель:</span><span>Solution Print</span></div>
      <div><span>Оригинальность:</span><span>Совместимый</span></div>'''))


def test_empty_filter_result_requires_explicit_supplier_message():
    assert source.confirmed_empty(page('', extra='<p>Нет товаров по заданным параметрам.</p>'))
    assert not source.confirmed_empty(page(''))
    assert not source.confirmed_empty(page('', extra='<p>Подобрано 0 товаров</p>'))


def item(code="123", stock="В наличии Склад Спб - 7 шт. Склад Мск - 11 шт.",
         price="86 р.", name="Картридж SP", extra=""):
    return f'''<div data-entity="item"><span class="product-cart__item-code">Код товара: {code}</span>
    <div class="product-cart__title"><a href="/catalog/cartridge-{code.replace('/', '-')}/">{name}</a></div>
    <div class="product-status">{stock}</div><span class="new-price">{price}</span>
    <span class="new-price new-price_second new-price_inline">999 р.</span>{extra}</div>'''


def page(body, city="Москва", extra=""):
    return source.document(f'''<html><body class="b2b-body"><div class="hdr-top__change-city">Ваш город: {city}</div>
    <div data-entity="container-1">{body}</div>{extra}</body></html>'''.encode())


def test_two_stocks_customer_price_and_recommendations():
    rows = source.parse_listing(page(item(), extra=item("999", price="900 р.")))
    assert len(rows) == 1
    assert (rows[0]["msk"], rows[0]["spb"], rows[0]["price"], rows[0]["currency"]) == (11, 7, "86", "RUB")


@pytest.mark.parametrize("status", ["Под заказ ~ 15.10.2026", "Нет в наличии"])
def test_order_only_is_zero(status):
    row = source.parse_listing(page(item(stock=status)))[0]
    assert row["msk"] == row["spb"] == 0


@pytest.mark.parametrize("status", ["В наличии более 10 шт.", "Склад Мск - 5 шт.",
                                  "Склад Спб - 2 шт. Склад Мск - 3 шт. Склад Мск - 4 шт."])
def test_unknown_or_partial_stock_stops_full_list(status):
    with pytest.raises(RuntimeError):
        source.parse_listing(page(item(stock=status)))


def test_requires_moscow_and_wholesale_authorization():
    with pytest.raises(RuntimeError, match="Moscow"):
        source.parse_listing(page(item(), city="Санкт-Петербург"))
    doc = page(item())
    doc.xpath('//body')[0].set("class", "retail")
    with pytest.raises(RuntimeError, match="B2B"):
        source.parse_listing(doc)


def test_usd_fallback_is_explicit_and_not_rrp():
    row = source.parse_listing(page(item(price="нет цены", extra='''
        <span class="new-price new-price_second">1.25<b class="dollar-icon"></b></span>''')))[0]
    assert (row["price"], row["currency"]) == ("1.25", "USD")


def test_unrecognized_price_rejected_instead_of_rrp():
    with pytest.raises(RuntimeError, match="price"):
        source.parse_listing(page(item(price="По запросу")))


def test_last_page_active_number_counts():
    doc = page(item(), extra='<a href="?PAGEN_1=267">267</a><strong class="pagin_active">268</strong>')
    assert source.page_count(doc) == 268


def test_fractional_stock_is_preserved_exactly():
    rows = source.parse_listing(page(item(stock="В наличии Склад Мск - 65.44 шт. Склад Спб - 3 шт.")))
    normalized, _ = run.normalize({"complete": True, "region": "Москва", "rows": rows}, datetime.now(timezone.utc))
    assert normalized[0]["qty"] == 68.44


def test_hidden_zero_warehouse_requires_independent_total():
    extra = "<script>new JCCatalogItem({'PRODUCT':{'XML_ID':'123','MAX_QUANTITY':'121'}});</script>"
    rows = source.parse_listing(page(item(stock="В наличии Склад Мск - 121 шт."), extra=extra))
    assert rows[0]["msk"] == 121 and rows[0]["spb"] == 0
    with pytest.raises(RuntimeError, match="confirmed zero"):
        source.parse_listing(page(item(stock="В наличии Склад Мск - 120 шт."), extra=extra))


def test_composite_code_is_preserved_not_split_or_summed():
    row = source.parse_listing(page(item(code="123/456")))[0]
    assert row["supplier_code"] == "123/456"


def test_normalize_sum_defect_and_cbr_full_precision():
    rows = source.parse_listing(page(item() + item("456", name="БРАК Картридж")))
    rows.append({**rows[0], "supplier_code": "789", "price": "12.34", "currency": "USD"})
    snap = {"complete": True, "region": "Москва", "rows": rows}
    def rate(day, currency, markup):
        assert markup == "1.03"
        return (Decimal("83.562973"), "12.10.2026") if currency == "USD" else (Decimal(1), None)
    with patch.object(run, "effective_rate", side_effect=rate):
        normalized, _ = run.normalize(snap, datetime.now(timezone.utc))
    assert normalized[0]["qty"] == 18
    assert normalized[1]["qty"] == 0
    assert normalized[2]["price_raw"] == "1031.17"
    assert normalized[0]["article"] == "SP123_MSK"


def test_normalize_rejects_duplicate_codes():
    row = source.parse_listing(page(item()))[0]
    with pytest.raises(RuntimeError, match="Duplicate"):
        run.normalize({"complete": True, "region": "Москва", "rows": [row, row]}, datetime.now(timezone.utc))


def test_zero_usd_price_does_not_require_cbr():
    row = {"supplier_code": "123", "name": "Картридж", "price": "0", "currency": "USD", "msk": 0, "spb": 0}
    with patch.object(run, "effective_rate", side_effect=AssertionError("Unnecessary FX request")):
        rows, rates = run.normalize({"complete": True, "region": "Москва", "rows": [row]}, datetime.now(timezone.utc))
    assert rows[0]["price_raw"] == "0" and rates == {}


def enter(name, doc_id="old", applicable=True):
    return {"id": doc_id, "name": name, "updated": "before", "applicable": applicable,
            "store": run.ms_api.ref("store", run.STORE_REMOTE),
            "organization": run.ms_api.ref("organization", run.ORG_DIGITAL),
            "positions": [{"assortment": run.ms_api.ref("product", "product-1"), "quantity": 18, "price": 8600}]}


def test_deletion_scope_is_strict():
    assert run.owned_doc(enter("s_print_msk_2026-10-12_p1"))
    assert not run.owned_doc(enter("s_print_spb_2026-08-20_p1"))
    assert not run.owned_doc(enter("unrelated_s_print_msk_2026-10-12_p1"))
    doc = enter("s_print_msk_2026-10-12_p1")
    doc["store"] = run.ms_api.ref("store", "our-stock")
    assert not run.owned_doc(doc)
    doc["store"] = run.ms_api.ref("store", run.STORE_REMOTE)
    doc["organization"] = run.ms_api.ref("organization", "other-company")
    assert not run.owned_doc(doc)


class FakeMS:
    def __init__(self):
        self.docs = {"old": enter("s_print_msk_2026-10-12_p1"),
                     "spb": enter("s_print_spb_2026-08-20_p1", "spb")}
        self.events = []
        self.fail_posting = False
        self.fail_delete_response = False

    def get(self, path, params=None):
        doc_id = path.rsplit("/", 1)[-1]
        if doc_id not in self.docs:
            raise run.ms_api.MsError(404, {})
        return copy.deepcopy(self.docs[doc_id])

    def iter_rows(self, path, params=None, page=20):
        if path.endswith("/positions"):
            return iter(copy.deepcopy(self.docs[path.split("/")[-2]]["positions"]))
        filters = (params or {}).get("filter", "")
        if isinstance(filters, list):
            return iter(copy.deepcopy([d for d in self.docs.values() if d["name"].startswith("s_print_msk_")]))
        name = filters.removeprefix("name=")
        return iter(copy.deepcopy([d for d in self.docs.values() if d["name"] == name]))

    def post(self, path, payload):
        assert path == "/entity/enter"
        self.events.append("create_draft")
        doc = copy.deepcopy(payload)
        doc["id"] = f"new-{len(self.docs)}"
        doc["updated"] = "new"
        self.docs[doc["id"]] = doc
        return copy.deepcopy(doc)

    def put(self, path, payload):
        doc_id = path.rsplit("/", 1)[-1]
        if payload.get("applicable"):
            self.events.append("post_enter")
            if self.fail_posting:
                raise RuntimeError("simulated post failure")
        self.docs[doc_id].update(payload)
        return copy.deepcopy(self.docs[doc_id])

    def delete(self, path):
        doc_id = path.rsplit("/", 1)[-1]
        self.events.append("delete_old")
        del self.docs[doc_id]
        if self.fail_delete_response:
            self.fail_delete_response = False
            raise RuntimeError("lost delete response")


def frozen_plan(fake):
    old = copy.deepcopy(fake.docs["old"])
    old["saved_positions"] = old.pop("positions")
    spec = enter("s_print_msk_2026-10-12_p1_old20261012", applicable=False)
    spec.pop("id")
    spec.pop("updated")
    spec["final_name"] = "s_print_msk_2026-10-12_p1"
    return {"run_id": "run1", "moment": "2026-10-12T20:00:00+00:00", "stale": [old], "docs": [spec],
            "ready": [], "updates": [], "skipped": [], "summary": {"source_rows": 1, "sum_rub": "1548"}}


@pytest.fixture
def fake_ms(monkeypatch):
    fake = FakeMS()
    for name in ("get", "iter_rows", "post", "put", "delete"):
        monkeypatch.setattr(run.ms_api, name, getattr(fake, name))
    monkeypatch.setattr(run, "wait_other_writers", lambda: None)
    monkeypatch.setattr(run.journal, "save", lambda *a: 42)
    monkeypatch.setattr(run, "query", lambda *a: [])
    monkeypatch.setattr(run.catalog, "sync", lambda *a: None)
    return fake


def test_post_failure_preserves_previous_stock_then_resume(fake_ms, tmp_path):
    fake_ms.fail_posting = True
    plan = frozen_plan(fake_ms)
    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="simulated"):
        run.apply(plan, tmp_path, state)
    assert "old" in fake_ms.docs and fake_ms.docs["old"]["applicable"]
    assert "delete_old" not in fake_ms.events
    fake_ms.fail_posting = False
    run.apply(plan, tmp_path, state)
    assert "old" not in fake_ms.docs and "spb" in fake_ms.docs
    assert fake_ms.events.count("create_draft") == 1
    assert json.loads(state.read_text())["status"] == "complete"


def test_lost_delete_response_recovers_without_duplicate_stock(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    fake_ms.fail_delete_response = True
    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="lost delete"):
        run.apply(plan, tmp_path, state)
    run.apply(plan, tmp_path, state)
    assert len(fake_ms.docs) == 2
    assert fake_ms.events.count("create_draft") == 1
    assert fake_ms.events.index("post_enter") < fake_ms.events.index("delete_old")
    assert json.loads(state.read_text())["load_id"] == 42


def test_changed_old_document_stops_before_any_write(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    fake_ms.docs["old"]["updated"] = "someone edited"
    with pytest.raises(RuntimeError, match="changed"):
        run.apply(plan, tmp_path, tmp_path / "state.json")
    assert fake_ms.events == []


def pending_state(tmp_path, created):
    return {"status": "pending", "run_id": "run1", "out": str(tmp_path),
            "created": created, "cards": ["verified-card"], "deleted": []}


def test_manual_import_retires_missing_drafts_without_ms_writes(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    state = pending_state(tmp_path, {"0": "deleted-draft"})
    path = tmp_path / "state.json"
    assert run.superseded_pending(plan, state, path)
    assert json.loads(path.read_text())["status"] == "superseded"
    assert json.loads((tmp_path / "superseded_state.json").read_text()) == state
    assert fake_ms.events == []
    assert "old" in fake_ms.docs and "spb" in fake_ms.docs


def test_partial_external_deletion_stops_without_retiring(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    state = pending_state(tmp_path, {"0": "old", "1": "deleted-draft"})
    with pytest.raises(RuntimeError, match="Some staged"):
        run.superseded_pending(plan, state, tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()
    assert fake_ms.events == []


def test_missing_drafts_without_replacement_require_review(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    del fake_ms.docs["old"]
    with pytest.raises(RuntimeError, match="without a complete"):
        run.superseded_pending(plan, pending_state(tmp_path, {"0": "missing"}), tmp_path / "state.json")
    assert fake_ms.events == []


def test_intact_pending_is_resumed(fake_ms, tmp_path):
    assert not run.superseded_pending(frozen_plan(fake_ms),
                                     pending_state(tmp_path, {"0": "old"}), tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()


def test_external_import_during_pending_stops_before_writes(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    fake_ms.fail_posting = True
    path = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="simulated"):
        run.apply(plan, tmp_path, path)
    fake_ms.docs["manual"] = enter("s_print_msk_2026-10-14_p1", "manual")
    fake_ms.events.clear()
    with pytest.raises(RuntimeError, match="replaced externally"):
        run.apply(plan, tmp_path, path)
    assert fake_ms.events == []


def test_unrecorded_draft_prevents_abandoning_run(fake_ms, tmp_path):
    plan = frozen_plan(fake_ms)
    fake_ms.docs["unsaved"] = enter(plan["docs"][0]["name"], "unsaved", applicable=False)
    with pytest.raises(RuntimeError, match="Unrecorded staged"):
        run.superseded_pending(plan, pending_state(tmp_path, {"0": "missing"}), tmp_path / "state.json")
    assert fake_ms.events == []


def test_api_error_is_not_mistaken_for_deleted_draft(fake_ms, monkeypatch, tmp_path):
    def forbidden(path):
        raise run.ms_api.MsError(403, {})
    monkeypatch.setattr(run.ms_api, "get", forbidden)
    with pytest.raises(run.ms_api.MsError):
        run.superseded_pending(frozen_plan(fake_ms), pending_state(tmp_path, {"0": "missing"}),
                               tmp_path / "state.json")
    assert not (tmp_path / "state.json").exists()

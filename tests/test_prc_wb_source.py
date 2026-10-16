# поток: prc
"""Regression checks for novelty source parsing, stale forms and WB provenance."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from prices import ms_import, wb_source
from prices.wb_label import RULES_VERSION
from tools.prc.wb_fill import compose_details
from tools.prc.wb_source_audit import check

TN = "Картридж для Brother TN2355 HL L2320D/MFC L2700D 2.6K White Box (Совместимый)"
KINDS = ["Комплект картриджей", "Тонер-картридж", "Картридж", "Фотобарабан", "Тонер", "Чернила"]


def tc(**over):
    return {**dict(consumable_type="Картридж", title="071H", additional_title="5646C002",
                printer_models=[{"brand": "Canon"}], ink_colors=["черный"],
                chip="С чипом", capacity="Увеличенная"), **over}


def record(source, **over):
    return {"Код": "9999wb", "Внешний код": "9999", "Артикул": "WB TN2355",
            "Наименование": TN, "Описание": TN, "Группы": "Тест", "Вес": "",
            "Закупочная цена": "", "Штрихкод Code128": "", "Доп. поле: Название WB": source["name"],
            "_wb_source_code": source["source_code"], "_wb_price_name": source["price_name"],
            "_wb_rules_version": source["rules_version"], "_wb_source_token": source["token"],
            "_wb_facts": source["facts"], "_wb_evidence": source["evidence"], **over}


class SupplierSource(unittest.TestCase):
    def setUp(self):
        self.query = patch.object(ms_import, "query", return_value=[])
        self.kinds = patch.object(ms_import, "tc_kinds", return_value=KINDS)
        self.query.start(); self.kinds.start()
        self.addCleanup(self.query.stop); self.addCleanup(self.kinds.stop)

    def test_real_tn2355_no_printer_or_white_box_color(self):
        s = ms_import.wb_details("", TN)
        self.assertEqual(s["name"], "Картридж TN2355 для Brother")
        self.assertEqual(s["raw"]["ink_colors"], [])
        self.assertEqual(s["raw"]["chip"], "")
        self.assertEqual(s["rules_version"], RULES_VERSION)
        self.assertEqual(s["name"], compose_details(s["raw"], TN).name)

    def test_printer_codes_resource_and_ambiguous_models_are_held(self):
        for tail in ("HL L2320D/MFC L2700D", "MFC-L2700D", "2.6K White Box",
                     "TN2355 TN2375", "TN2355/TN2375"):
            with self.subTest(tail=tail):
                self.assertFalse(ms_import.wb_details("", "Картридж для Brother " + tail)["name"])

    def test_after_for_family_must_match_kind(self):
        self.assertEqual(ms_import.wb_details("", "Фотобарабан для Brother DR-2335 HL L2320D")["raw"]["title"], "DR-2335")
        self.assertFalse(ms_import.wb_details("", "Картридж для Brother DR-2335 HL L2320D")["name"])

    def test_chip_counter_qualification_is_preserved(self):
        s = ms_import.wb_details("", "Картридж Brother TN2355, с чипом без счетчика")
        self.assertIn("без счётчика", s["name"])

    def test_confirmed_color_requires_evidence_and_does_not_change_model(self):
        before = ms_import.wb_details("", TN)
        denied = ms_import.wb_details("", TN, {"color": "черный"})
        self.assertFalse(denied["name"])
        self.assertIn("источник", denied["reason"])
        good = ms_import.wb_details("", TN, {"color": "черный"}, "Карточка поставщика, цвет Black")
        self.assertEqual(good["name"], "Картридж TN2355 для Brother черный")
        self.assertNotEqual(before["token"], good["token"])

    def test_missing_model_can_be_confirmed_without_free_title(self):
        text = "Картридж для Brother HL L2320D"
        s = ms_import.wb_details("", text, {"model": "TN2355"}, "OEM-код в спецификации поставщика")
        self.assertEqual(s["name"], "Картридж TN2355 для Brother")
        s = ms_import.wb_details("", text, {"model": "Красивый картридж для дома"}, "Источник")
        self.assertFalse(s["name"])

    def test_confirmed_data_cannot_replace_known_fact_or_unknown_vocabulary(self):
        for facts in ({"model": "TN2375"}, {"brand": "Canon"}, {"color": "абракадабра"},
                      {"chip": "неизвестный"}, {"capacity": "огромный"}, {"random": "foo"}):
            with self.subTest(facts=facts):
                self.assertFalse(ms_import.wb_details("", TN, facts, "Документ поставщика")["name"])

    def test_changed_source_or_version_blocks_whole_batch_before_api(self):
        s = ms_import.wb_details("", TN)
        good = record(s)
        for bad in (dict(good, _wb_rules_version="old"),
                    dict(good, _wb_source_token="stale"),
                    dict(good, _wb_price_name=TN + " XL")):
            with self.subTest(bad=bad), patch.object(ms_import, "_folders") as folders:
                with self.assertRaises(ValueError):
                    ms_import.create_in_ms([good, bad], "supplier", dry=False)
                folders.assert_not_called()

    def test_excel_keeps_source_and_uses_shared_validation(self):
        s = ms_import.wb_details("", TN)
        rec = record(s)
        with tempfile.TemporaryDirectory() as tmp:
            path = ms_import.write_xlsx([rec], Path(tmp)/"cards.xlsx")
            import openpyxl
            book = openpyxl.load_workbook(path, data_only=True)
            sheet = book.active
            restored = dict(zip([c.value for c in sheet[1]], [c.value for c in sheet[2]]))
            book.close()
        ms_import.prepare_wb_import([restored])
        self.assertEqual(restored["_wb_source_code"], "")
        self.assertEqual(restored["_wb_resolved"]["raw"], s["raw"])
        self.assertEqual(restored["Доп. поле: Название WB"], s["name"])
        restored["Доп. поле: Название WB"] = "Произвольное название"
        with self.assertRaises(ValueError):
            ms_import.prepare_wb_import([restored])

    def test_dry_creation_does_not_journal_or_post(self):
        s = ms_import.wb_details("", TN)
        from core import ms_api
        with patch.object(ms_import, "_folders", return_value={"Тест":"folder"}), \
             patch.object(ms_api, "get", return_value={"rows":[]}), \
             patch.object(ms_api, "post") as post, patch.object(wb_source, "prepare_record") as save:
            made, _ = ms_import.create_in_ms([record(s)], "supplier", dry=True, log=lambda *a: None)
        self.assertEqual(made, [])
        save.assert_not_called(); post.assert_not_called()

    def test_source_journal_precedes_post_and_does_not_enter_ms_body(self):
        s = ms_import.wb_details("", TN)
        from core import ms_api
        calls = Mock()
        calls.post.return_value = {"id":"product-id"}
        with patch.object(ms_import, "_folders", return_value={"Тест":"folder"}), \
             patch.object(ms_api, "get", return_value={"rows":[]}), \
             patch.object(ms_api, "post", calls.post), \
             patch.object(wb_source, "prepare_record", calls.prepare), \
             patch.object(wb_source, "mark_created", calls.mark):
            ms_import.create_in_ms([record(s)], "supplier", dry=False, log=lambda *a: None)
        self.assertEqual([c[0] for c in calls.mock_calls], ["prepare","post","mark"])
        body = calls.post.call_args.args[1]
        self.assertFalse(any(k.startswith("_wb") for k in body))
        self.assertEqual(body["externalCode"], "9999")

    def test_source_persistence_failure_prevents_post(self):
        s = ms_import.wb_details("", TN)
        from core import ms_api
        with patch.object(ms_import, "_folders", return_value={"Тест":"folder"}), \
             patch.object(ms_api, "get", return_value={"rows":[]}), \
             patch.object(ms_api, "post") as post, \
             patch.object(wb_source, "prepare_record", side_effect=RuntimeError("DB unavailable")):
            with self.assertRaises(RuntimeError):
                ms_import.create_in_ms([record(s)], "supplier", dry=False, log=lambda *a: None)
        post.assert_not_called()


class CatalogueAndSavedSource(unittest.TestCase):
    def test_unrelated_catalogue_fields_do_not_invalidate_dialog(self):
        raw = tc()
        with patch.object(ms_import,"query",return_value=[{"raw":raw}]):
            before = ms_import.wb_details("1234", "Canon 071H")
            raw["weight"] = "1000"
            raw["supplier_price"] = 1234
            after = ms_import.wb_details("1234", "Canon 071H")
        self.assertEqual(before["token"],after["token"])

    def test_new_code_excel_and_existing_card_use_original_catalogue_model(self):
        raw = tc()
        with patch.object(ms_import, "query", return_value=[{"raw":raw}]) as query:
            s = ms_import.wb_details("1234", "Canon 071H XL")
            rec = record(s)
            self.assertEqual(ms_import.wb_record_errors([rec]), [])
            saved = dict(rec["_wb_resolved"])
            product = {"id":"p", "code":"9999wb", "externalCode":"9999", "name":TN,
                       "attributes":[{"name":"Название WB", "value":s["name"]}]}
            with patch.object(wb_source.db, "query", return_value=[
                {"ms_id":"p", "external_code":"9999", "source":saved}]):
                result = check(product)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["source_code"], "1234")
            self.assertEqual(result["expected"], compose_details(raw, "Canon 071H XL").name)
            self.assertTrue(all(c.args[1] == ("1234",) for c in query.call_args_list))

    def test_invalid_catalogue_cannot_be_bypassed_with_confirmation(self):
        raw = tc(); raw["chip"] = "???"
        with patch.object(ms_import, "query", return_value=[{"raw":raw}]), \
             patch.object(ms_import, "wb_from_price") as fallback:
            s = ms_import.wb_details("1234", TN, {"chip":"Без чипа"}, "Источник")
        self.assertFalse(s["name"]); fallback.assert_not_called()

    def test_same_confirmed_facts_reproduce_saved_supplier_name(self):
        with patch.object(ms_import, "query", return_value=[]), \
             patch.object(ms_import, "tc_kinds", return_value=KINDS):
            s = ms_import.wb_details("", TN, {"color":"черный"}, "Источник Black")
            s["card_name"] = TN
            row = {"ms_id":"p", "external_code":"9999", "source":s}
            with patch.object(wb_source.db,"query",return_value=[row]):
                actual = wb_source.for_product({"id":"p","externalCode":"9999","code":"9999wb","name":TN})
        self.assertEqual(actual["name"],s["name"])
        self.assertEqual(actual["token"],s["token"])

    def test_source_cannot_attach_to_another_card_or_changed_name(self):
        row = {"ms_id":"p", "external_code":"9999", "source":{"card_name":TN}}
        product = {"id":"p", "externalCode":"9999", "code":"9999wb", "name":TN}
        for changed in ({"id":"q"}, {"externalCode":"9998"}, {"name":"Другая модель"}):
            with self.subTest(changed=changed), patch.object(wb_source.db,"query",return_value=[row]):
                with self.assertRaises(ValueError):
                    wb_source.for_product({**product,**changed})

    def test_source_preparation_refuses_to_replace_created_record(self):
        with patch.object(wb_source.db,"execute",return_value=0), self.assertRaises(ValueError):
            wb_source.prepare_record({"Код":"9999wb","Внешний код":"9999"},{"token":"t"})


class PreviewRoute(unittest.TestCase):
    def test_preview_reads_original_name_and_never_writes(self):
        tree = ast.parse(Path("web/app.py").read_text())
        node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="novelties_wb_name_preview")
        node.decorator_list=[]; node.args.args[0].annotation=None
        db = SimpleNamespace(query=Mock(return_value=[{"name":TN,"ms_code":None}]),execute=Mock())
        ns={"db":db}
        exec(compile(ast.Module(body=[node],type_ignores=[]),"web/app.py","exec"),ns)
        with patch.object(ms_import,"query",return_value=[]), \
             patch.object(ms_import,"tc_kinds",return_value=KINDS):
            result=ns["novelties_wb_name_preview"](SimpleNamespace(id=18958,source_code="",facts={},evidence=""))
        self.assertEqual(result["wb_source"]["name"],"Картридж TN2355 для Brother")
        db.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

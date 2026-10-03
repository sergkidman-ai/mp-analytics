"""Поток card: безопасный подбор совместимости, без API-записи и БД."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import ozon_card_compat as compat


def attr(aid, vals):
    return {"id": aid, "complex_id": 100007,
            "values": [{"dictionary_value_id": i, "value": name} for i, name in vals]}


def card(offer="0279X3ABCDEF", models=((10, "ML-3310D"),), brands=None, oem="MLT-D205L"):
    if brands is None:
        brands = [(1, "Samsung")] * len(models)
    return {"id": 123, "offer_id": offer, "description_category_id": 17028935,
            "type_id": 95980,
            "attributes": [{"id": 12141, "complex_id": 0,
                            "values": [{"dictionary_value_id": 0, "value": oem}]}],
            "complex_attributes": [attr(22888, brands)] +
                                  ([attr(22889, models)] if models else [])}


def info(warning=True, status="Продается", offer="0279X3ABCDEF"):
    return {"offer_id": offer, "statuses": {
        "status_name": status, "moderate_status": "approved", "status_description": ""},
        "errors": ([{"code": "attribute_hierarchy_fail", "attribute_id": 22889,
                     "level": "ERROR_LEVEL_WARNING"}] if warning else [])}


DICT = {22888: {1: "Samsung", 2: "Canon"},
        22889: {10: "ML-3310D", 11: "ML-3310ND", 20: "Pixma MX310"}}


class CompatibilitySafety(unittest.TestCase):
    def test_healthy_parent_replaces_obsolete_ids_and_adds_models(self):
        child = card(models=[(999, "ML-3310D")])
        donor = card("0279ABCDEFGH", models=[(10, "ML-3310D"), (11, "ML-3310ND")])
        result = compat.plan(child, info(), [(donor, info(False))], DICT)
        self.assertEqual(result["strategy"], "healthy_parent")
        self.assertEqual(result["source_offer"], "0279ABCDEFGH")
        self.assertEqual(compat.printer_keys(result["attributes"]), {"ML3310D", "ML3310ND"})

    def test_parent_without_models_does_not_fake_restoration(self):
        child = card(models=[], brands=[(1, "Samsung")])
        donor = card("0279ABCDEFGH", models=[], brands=[(1, "Samsung")])
        result = compat.plan(child, info(), [(donor, info(False))], DICT)
        self.assertEqual(result["strategy"], "repeat_brand_only")
        self.assertFalse(compat.printer_keys(result["attributes"]))

    def test_parent_cannot_remove_existing_printer(self):
        child = card(models=[(10, "ML-3310D"), (11, "ML-3310ND")])
        donor = card("0279ABCDEFGH")
        result = compat.plan(child, info(), [(donor, info(False))], DICT)
        self.assertEqual(result["strategy"], "repeat_existing")
        self.assertEqual(len(compat.printer_keys(result["attributes"])), 2)

    def test_wrong_parent_oem_category_or_health_not_used(self):
        for kind in ("oem", "category", "warning", "archive", "derived"):
            with self.subTest(kind=kind):
                donor = card("0279ABCDEFGH", models=[(10, "ML-3310D"), (11, "ML-3310ND")])
                state = info(False)
                if kind == "oem":
                    donor["attributes"][0]["values"][0]["value"] = "OTHER"
                elif kind == "category":
                    donor["type_id"] = 1
                elif kind == "warning":
                    state = info(True)
                elif kind == "archive":
                    state["is_archived"] = True
                else:
                    donor["offer_id"] = "02791ABCDEFG"
                result = compat.plan(card(), info(), [(donor, state)], DICT)
                self.assertEqual(result["strategy"], "repeat_existing")

    def test_conflicting_healthy_parents_are_reported(self):
        first = card("0279ABCDEFGH", models=[(10, "ML-3310D"), (11, "ML-3310ND")])
        second = card("0279BCDEFGHI")
        result = compat.plan(card(), info(), [(first, info(False)), (second, info(False))], DICT)
        self.assertEqual(result["strategy"], "skip")
        self.assertIn("разные списки", result["reason"])

    def test_balance_one_brand_keeps_models_and_input_unchanged(self):
        child = card(models=[(10, "ML-3310D"), (11, "ML-3310ND")],
                     brands=[(1, "Samsung")] * 3)
        original = copy.deepcopy(child)
        result = compat.plan(child, info(), dictionaries=DICT)
        self.assertEqual(result["strategy"], "balance_existing")
        self.assertEqual(len(result["attributes"][0]["values"]), 2)
        self.assertEqual(child, original)

    def test_multiple_brands_with_mismatched_counts_are_not_zipped(self):
        child = card(brands=[(1, "Samsung"), (2, "Canon")])
        result = compat.plan(child, info(), dictionaries=DICT)
        self.assertEqual(result["strategy"], "skip")
        self.assertIn("несколько брендов", result["reason"])

    def test_missing_id_and_same_id_changed_name_are_not_guessed(self):
        for models in ([(999, "ML-3310D")], [(10, "ML-3310ND")]):
            with self.subTest(models=models):
                result = compat.plan(card(models=models), info(), dictionaries=DICT)
                self.assertEqual(result["strategy"], "skip")

    def test_warning_for_another_attribute_is_out_of_scope(self):
        state = info()
        state["errors"][0]["attribute_id"] = 999
        self.assertEqual(compat.plan(card(), state, dictionaries=DICT)["strategy"], "skip")

    def test_hard_error_or_updating_card_is_not_modified(self):
        for kind in ("error", "updating", "declined"):
            with self.subTest(kind=kind):
                state = info()
                if kind == "error":
                    state["errors"].append({"level": "ERROR_LEVEL_ERROR"})
                elif kind == "updating":
                    state["statuses"]["status_description"] = "Обновляется"
                else:
                    state["statuses"]["moderate_status"] = "declined"
                self.assertEqual(compat.plan(card(), state, dictionaries=DICT)["strategy"], "skip")

    def test_verification_requires_import_saved_values_and_finished_processing(self):
        proposal = compat.plan(card(), info(), dictionaries=DICT)
        saved = card()
        self.assertTrue(compat.verify_result(proposal, saved, info(False), "imported"))
        self.assertFalse(compat.verify_result(proposal, saved, info(False), "skipped"))
        self.assertFalse(compat.verify_result(proposal, saved, info(), "imported"))
        self.assertFalse(compat.verify_result(proposal, card(models=[]), info(False), "imported"))
        updating = info(False)
        updating["statuses"]["status_description"] = "Обновляется"
        self.assertFalse(compat.verify_result(proposal, saved, updating, "imported"))

    def test_bundle_not_printer_derivative(self):
        self.assertEqual(compat.bundle_parent("2618X12EW6CF"), "2618")
        self.assertEqual(compat.bundle_parent("2618Х12"), "2618")
        self.assertIsNone(compat.bundle_parent("261812SMN12E"))
        self.assertFalse(compat.base_offer("2618X12EW6CF", "2618"))
        self.assertTrue(compat.base_offer("2618YVS70TZX", "2618"))

    def test_printer_suffixes_are_not_collapsed(self):
        self.assertNotEqual(compat.normal("ML-3310D"), compat.normal("ML-3310ND"))

    def test_signature_preserves_brand_model_pairs(self):
        first = [attr(22888, [(1, "Samsung"), (2, "Canon")]),
                 attr(22889, [(10, "ML-3310D"), (20, "Pixma MX310")])]
        reordered_pairs = [attr(22888, [(2, "Canon"), (1, "Samsung")]),
                           attr(22889, [(20, "Pixma MX310"), (10, "ML-3310D")])]
        wrong_pairs = [first[0], reordered_pairs[1]]
        self.assertEqual(compat.signature(first), compat.signature(reordered_pairs))
        self.assertNotEqual(compat.signature(first), compat.signature(wrong_pairs))

    def test_invalid_update_time_is_not_permission_to_write(self):
        for timestamp in ("invalid", "2026-01-01T10:00:00"):
            state = info()
            state["statuses"]["status_updated_at"] = timestamp
            self.assertTrue(compat.fresh(state))

    def test_dry_run_has_no_api_update_or_database_writes(self):
        from tools import ozon_card_push as push
        fake = unittest.mock.Mock()
        fake.attrs.return_value = [card()]
        fake.info.return_value = [info(False)]
        with patch.object(compat, "Client", return_value=fake), \
                patch.object(push, "execute") as write, patch.object(push, "_log") as log:
            compat.run_models("oz_acc2", 20, False, ["0279X3ABCDEF"])
        fake.post.assert_not_called()
        write.assert_not_called()
        log.assert_not_called()

    def test_apply_requires_explicit_small_list_before_any_api(self):
        with patch.object(compat, "Client") as client:
            with self.assertRaises(ValueError):
                compat.run_models("oz_acc2", 20, True)
            with self.assertRaises(ValueError):
                compat.run_models("oz_acc2", 100, True, [str(i) for i in range(21)])
        client.assert_not_called()

    def test_auto_rejects_large_batch_and_explicit_list_before_api(self):
        with patch.object(compat, "Client") as client:
            for limit, offers in ((21, None), (20, ["0279"]), (0, None)):
                with self.assertRaises(ValueError):
                    compat.run_models("oz_acc2", limit, True, offers, auto=True)
        client.assert_not_called()

    def test_daily_repairs_before_legacy_and_caps_batch(self):
        from tools import ozon_card_push as push
        calls = []
        with tempfile.TemporaryDirectory() as folder, \
                patch("builtins.open", side_effect=lambda *_: Path(folder, "lock").open("a")), \
                patch.object(compat, "run_models", side_effect=lambda *a, **k: calls.append("repair")) as repair, \
                patch.object(push, "run", side_effect=lambda *a: calls.append("legacy")) as legacy:
            compat.run_daily("oz_acc2", 500, ("A", "W"))
        self.assertEqual(calls, ["repair", "legacy"])
        repair.assert_called_once_with("oz_acc2", 20, True, auto=True)
        legacy.assert_called_once_with("oz_acc2", 500, True, ("A", "W"))

    def test_daily_failure_prevents_legacy_push(self):
        from tools import ozon_card_push as push
        with tempfile.TemporaryDirectory() as folder, \
                patch("builtins.open", side_effect=lambda *_: Path(folder, "lock").open("a")), \
                patch.object(compat, "run_models", side_effect=RuntimeError("not verified")), \
                patch.object(push, "run") as legacy:
            with self.assertRaisesRegex(RuntimeError, "not verified"):
                compat.run_daily("oz_acc2", 500, ("A", "W"))
        legacy.assert_not_called()

    def test_auto_empty_queue_only_reads_database_with_backoff(self):
        from core import db
        from tools import ozon_card_push as push
        with patch.object(db, "query", return_value=[]) as read, \
                patch.object(compat, "Client") as client, \
                patch.object(push, "execute") as write:
            self.assertEqual(compat.run_models("oz_acc2", 20, True, auto=True), [])
        sql, params = read.call_args.args
        self.assertIn("NOT needs_human", sql)
        self.assertIn("interval '20 hours'", sql)
        self.assertIn("err_class='W'", sql)
        self.assertEqual(params, ("oz_acc2", 20))
        client.assert_not_called()
        write.assert_not_called()

    def test_auto_skips_content_error_without_api_write(self):
        from core import db
        from tools import ozon_card_push as push
        fake = unittest.mock.Mock()
        fake.attrs.return_value = [card("0279")]
        bad = info(offer="0279")
        bad["errors"].append({"code": "DESCRIPTION_DECLINE", "level": "ERROR_LEVEL_ERROR"})
        fake.info.return_value = [bad]
        fake.dictionary.side_effect = lambda aid, _: DICT[aid]
        with patch.object(db, "query", return_value=[{"offer_id": "0279"}]), \
                patch.object(compat, "Client", return_value=fake), \
                patch.object(push, "execute") as write, patch.object(push, "_log") as log:
            result = compat.run_models("oz_acc2", 20, True, auto=True)
        self.assertEqual(result[0]["strategy"], "skip")
        fake.post.assert_not_called()
        write.assert_not_called()
        log.assert_not_called()

    def test_apply_sends_only_compatibility_and_verifies_saved_values(self):
        from tools import ozon_card_push as push
        before = card("0279", brands=[(1, "Samsung")] * 2)
        after = card("0279")
        fake = unittest.mock.Mock()
        fake.attrs.side_effect = [[before], [before], [after]]
        fake.info.side_effect = [[info(offer="0279")], [info(offer="0279")],
                                 [info(False, offer="0279")]]
        fake.dictionary.side_effect = lambda aid, _: DICT[aid]
        fake.post.side_effect = [{"task_id": 1}, {"result": {"items": [
            {"offer_id": "0279", "status": "imported"}]}}]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(compat, "Client", return_value=fake), \
                patch.object(compat, "Path", side_effect=lambda _: Path(folder)), \
                patch.object(push, "execute"), patch.object(push, "_log"), \
                patch.object(push, "_mark_attempt"), \
                patch.object(push, "_mark_warn_cleared") as healed:
            compat.run_models("oz_acc2", 1, True, ["0279"])
            self.assertTrue(list(Path(folder).glob("*/before.json")))
        endpoint, body = fake.post.call_args_list[0].args
        self.assertEqual(endpoint, "/v1/product/attributes/update")
        self.assertEqual({a["id"] for a in body["items"][0]["attributes"]}, {22888, 22889})
        self.assertEqual(len(body["items"][0]["attributes"][0]["values"]), 1)
        healed.assert_called_once_with("oz_acc2", "0279")

    def test_failed_import_stops_next_card_and_is_not_marked_healed(self):
        from tools import ozon_card_push as push
        first = card("0279")
        second = card("0283")
        fake = unittest.mock.Mock()
        fake.attrs.side_effect = [[first, second], [first]]
        fake.info.side_effect = [[info(offer="0279"), info(offer="0283")], [info(offer="0279")]]
        fake.dictionary.side_effect = lambda aid, _: DICT[aid]
        fake.post.side_effect = [{"task_id": 1}, {"result": {"items": [
            {"offer_id": "0279", "status": "skipped"}]}}]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(compat, "Client", return_value=fake), \
                patch.object(compat, "Path", side_effect=lambda _: Path(folder)), \
                patch.object(push, "execute"), patch.object(push, "_log"), \
                patch.object(push, "_mark_attempt"), \
                patch.object(push, "_mark_warn_cleared") as healed:
            with self.assertRaisesRegex(RuntimeError, "остальные карточки не отправлены"):
                compat.run_models("oz_acc2", 2, True, ["0279", "0283"])
        updates = [c for c in fake.post.call_args_list if c.args[0].endswith("/update")]
        self.assertEqual(len(updates), 1)
        healed.assert_not_called()


if __name__ == "__main__":
    unittest.main()

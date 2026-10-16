# поток: mkt
"""Safety checks for read-only bundle grouping preparation."""
import copy
import unittest
from unittest.mock import patch, Mock

from ops.wb_bundle_plan import (CARDS_URL, fetch_catalog, fingerprint,
                                is_bundle, plan_account, verify_preview, verify_result)


def card(nm, code, qty=1, imt=None, model="SP-230H", color="Черный", resource=3000):
    return {"nmID": nm, "vendorCode": code, "imtID": imt or nm * 10,
            "subjectID": 1820, "title": f"{'Комплект картриджей' if qty > 1 else 'Картридж'} {model} {qty} шт",
            "description": "Картридж без чипа", "characteristics": [
                {"id": 179792, "value": [str(qty)]},
                {"id": 5023, "value": [model]}, {"id": 58854, "value": [color]},
                {"id": 88963, "value": resource}]}


class BundlePlanTests(unittest.TestCase):
    def setUp(self):
        self.tc = {"3351": {"title": "SP-230H", "color": "BK", "resource": 3000,
                             "consumable_type": "Картридж", "chip": "nochip"}}
        self.chips = {"3351": ("без чипа", "ТК")}

    def plan(self, cards, account="wb_acc1"):
        return plan_account(account, cards, self.tc, self.chips)

    def test_random_suffix_of_single_product_is_not_a_bundle(self):
        self.assertFalse(is_bundle(card(1, "3351X12ABCDZ")))

    def test_cyrillic_x_is_recognized(self):
        rows, operations = self.plan([card(1, "3351"), card(2, "3351Х2", 2)])
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertEqual(operations[0]["body_preview"], {"targetIMT": 10, "nmIDs": [2]})

    def test_quantity_disagreement_blocks_transfer(self):
        rows, ops = self.plan([card(1, "3351"), card(2, "3351X6", 2)])
        self.assertEqual(rows[0]["status"], "review")
        self.assertFalse(ops)

    def test_field_bundle_with_wrong_single_title_stays_in_review(self):
        bundle = card(2, "3351X2", 2); bundle["title"] = "Картридж SP-230H 1 шт"
        rows, ops = self.plan([card(1, "3351"), bundle])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "review")
        self.assertFalse(ops)

    def test_black_and_photo_black_are_different(self):
        rows, ops = self.plan([card(1, "3351"), card(2, "3351X2", 2, color="Фоточерный")])
        self.assertIn("цвет бандла отличается от основной", rows[0]["reasons"])
        self.assertFalse(ops)

    def test_chip_conflict_blocks_transfer(self):
        self.chips["3351"] = ("противоречие", "разные поставщики")
        rows, ops = self.plan([card(1, "3351"), card(2, "3351X2", 2)])
        self.assertFalse(ops)
        self.assertIn("чип: противоречие", rows[0]["reasons"])

    def test_existing_good_group_is_not_split(self):
        cards = [card(1, "3351"), card(2, "3351X2", 2, imt=20), card(3, "1111", imt=20)]
        rows, ops = self.plan(cards)
        self.assertIn("в исходной группе есть другие карточки", rows[0]["reasons"])
        self.assertFalse(ops)

    def test_old_four_card_rule_is_not_used(self):
        cards = [card(1, "3351", imt=10), card(2, "3351X2", 2)]
        cards += [card(i, f"111{i}", imt=10) for i in range(3, 7)]
        rows, ops = self.plan(cards)
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertEqual(ops[0]["future_group_size"], 6)

    def test_limit_includes_existing_target_members(self):
        cards = [card(1, "3351", imt=10), card(2, "3351X2", 2)]
        cards += [card(i, f"other-{i}", imt=10) for i in range(3, 32)]
        rows, ops = self.plan(cards)
        self.assertFalse(ops)
        self.assertEqual(rows[0]["status"], "review")

    def test_already_linked_does_not_generate_request(self):
        rows, ops = self.plan([card(1, "3351", imt=10), card(2, "3351X2", 2, imt=10)])
        self.assertEqual(rows[0]["status"], "already_linked")
        self.assertFalse(ops)

    def test_diskver_primary_with_suffix_is_found(self):
        rows, ops = self.plan([card(1, "3351F1URWQ78"), card(2, "3351X2ABCD12", 2)], "wb_acc2")
        self.assertEqual(rows[0]["parent_nm"], 1)
        self.assertEqual(len(ops), 1)

    def test_diskver_printer_derivative_is_not_mistaken_for_primary(self):
        cards = [card(1, "3351F1URWQ78"), card(3, "33512ABCDEFG", model="Ricoh SP 230SFNw"),
                 card(2, "3351X2ABCD12", 2)]
        rows, ops = self.plan(cards, "wb_acc2")
        self.assertEqual(rows[0]["parent_nm"], 1)
        self.assertEqual(len(rows[0]["parent_candidates"]), 2)
        self.assertEqual(len(ops), 1)

    def test_changed_group_or_product_rejects_previous_plan(self):
        cards = [card(1, "3351"), card(2, "3351X2", 2)]
        _, ops = self.plan(cards)
        self.assertTrue(verify_preview(ops[0], cards)["ok"])
        self.assertFalse(verify_preview(ops[0], cards + [card(3, "1111", imt=10)])["ok"])
        changed = copy.deepcopy(cards); changed[1]["description"] = "с чипом"
        self.assertFalse(verify_preview(ops[0], changed)["ok"])
        self.assertFalse(verify_preview(ops[0], cards[:1])["ok"])

    def test_api_characteristic_order_does_not_change_fingerprint(self):
        original = [card(1, "3351")]; reordered = copy.deepcopy(original)
        reordered[0]["characteristics"].reverse()
        self.assertEqual(fingerprint(original), fingerprint(reordered))

    def test_reader_uses_only_read_only_content_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"cards": [card(1, "3351")]}
        with patch("ops.wb_bundle_plan._content_token", return_value="test"), patch(
                "ops.wb_bundle_plan.requests.post", return_value=response) as post:
            result = fetch_catalog("wb_acc1", text_search="3351")
        self.assertEqual(len(result), 1)
        self.assertEqual(post.call_args.args[0], CARDS_URL)
        self.assertEqual(post.call_args.kwargs["json"]["settings"]["filter"]["textSearch"], "3351")

    def test_pagination_keeps_latest_observation_when_card_is_updated(self):
        first = Mock(status_code=200)
        first.json.return_value = {"cards": [card(i, f"other-{i}") for i in range(1, 101)],
                                   "cursor": {"updatedAt": "2026-10-04", "nmID": 100}}
        second = Mock(status_code=200)
        updated = card(1, "3351", imt=999)
        second.json.return_value = {"cards": [updated]}
        with patch("ops.wb_bundle_plan._content_token", return_value="test"), patch(
                "ops.wb_bundle_plan.requests.post", side_effect=[first, second]), patch("ops.wb_bundle_plan.time.sleep"):
            result = fetch_catalog("wb_acc1")
        self.assertEqual(len(result), 100)
        self.assertEqual(next(c for c in result if c["nmID"] == 1)["imtID"], 999)

    def test_result_requires_all_bundles_and_parent_in_target_group(self):
        cards = [card(1, "3351"), card(2, "3351X2", 2)]
        _, ops = self.plan(cards)
        self.assertFalse(verify_result(ops[0], cards)["ok"])
        cards[1]["imtID"] = 10
        self.assertTrue(verify_result(ops[0], cards)["ok"])
        self.assertFalse(verify_result(ops[0], cards[:1])["ok"])
        cards[0]["imtID"] = 99
        self.assertFalse(verify_result(ops[0], cards)["ok"])

    def test_glue_reader_filters_exact_nm_and_uses_account(self):
        from ops.wb_glue import live
        with patch("ops.wb_glue.fetch_catalog", return_value=[card(1, "3351"), card(2, "3351X2", 2)]) as fetch:
            result = live([1, 1], "wb_acc2")
        self.assertEqual(set(result), {1})
        fetch.assert_called_once_with("wb_acc2", text_search=1)

    def test_http_200_with_wb_error_is_not_success(self):
        from ops.wb_glue import check_move_response
        response = Mock(content=b'{}'); response.json.return_value = {"error": True}
        with self.assertRaises(RuntimeError):
            check_move_response(response)
        response.json.return_value = {"error": False}
        check_move_response(response)


if __name__ == "__main__":
    unittest.main()

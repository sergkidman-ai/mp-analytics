# поток: mkt / ozon
"""Safety boundaries for the dedicated account2 stop-only guard; no network."""
import copy
import datetime as dt
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path('/opt/mp-analytics')
REPORT = ROOT / 'docs/reports/ozon_acc2_monthly_experiment_20261108'
spec = importlib.util.spec_from_file_location('acc2_guard', ROOT / 'ops/ozon_acc2_experiment_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
CONFIG = json.loads((REPORT / 'risk_policy.json').read_text())


class Safety(unittest.TestCase):
    def test_decision_boundaries_and_unknown_orders(self):
        p = CONFIG['risk_policy']
        for spend, complete, expected in [
            ('299.99', True, None), ('300', True, 'ZERO_OWN_AND_HALO_ORDERS'),
            ('999.99', True, 'ZERO_OWN_AND_HALO_ORDERS'),
            ('1000', True, 'ZERO_CONFIRMED_ORDERS_1000'),
            ('499.99', False, None), ('500', False, 'PROVISIONAL_STOP_SELLER_ORDERS_UNAVAILABLE'),
            ('10000', False, 'PROVISIONAL_STOP_SELLER_ORDERS_UNAVAILABLE')]:
            with self.subTest(spend=spend, complete=complete):
                self.assertEqual(guard.decision(D(spend), 0, 0, 0, 0, complete, p), expected)

    def test_any_confirmed_own_or_related_order_protects(self):
        for orders in [(1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1)]:
            self.assertIsNone(guard.decision(D('5000'), *orders, True, CONFIG['risk_policy']))

    def test_binding_account_hash_and_forbidden_old_id(self):
        original = guard.validate_bindings(CONFIG)
        self.assertEqual(len(original['campaigns']), 4)
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'bindings.json'
            config = {**CONFIG, 'bindings_path': str(file)}
            variants = []
            b = copy.deepcopy(original); b['account'] = 'oz_acc1'; variants.append(b)
            b = copy.deepcopy(original); b['campaigns'][0]['campaign_id'] = b['forbidden_existing_campaign_ids'][0]; variants.append(b)
            b = copy.deepcopy(original); b['activation_allowed'] = True; variants.append(b)
            b = copy.deepcopy(original); b['campaigns'].pop(); variants.append(b)
            for b in variants:
                file.write_text(json.dumps(b))
                config['bindings_sha256'] = hashlib.sha256(file.read_bytes()).hexdigest()
                with self.assertRaises(AssertionError):
                    guard.validate_bindings(config)
            file.write_text(json.dumps(original))
            config['bindings_sha256'] = 'bad_hash'
            with self.assertRaises(AssertionError):
                guard.validate_bindings(config)

    def test_api_rejects_activation_old_campaign_edit_and_removal(self):
        b = guard.validate_bindings(CONFIG)
        api = object.__new__(guard.Api)
        api.ids = {x['campaign_id'] for x in b['campaigns']}
        new = sorted(api.ids)[0]
        old = b['forbidden_existing_campaign_ids'][0]
        for method, path in [('POST', f'/api/client/campaign/{new}/activate'),
                             ('POST', f'/api/client/campaign/{old}/deactivate'),
                             ('PUT', f'/api/client/campaign/{new}'),
                             ('POST', f'/api/client/campaign/{new}/products'),
                             ('DELETE', f'/api/client/campaign/{new}/products')]:
            with self.subTest(path=path), self.assertRaises(AssertionError):
                api.call(method, path)

    def test_weekly_reference_uses_activation_date_and_moscow_boundary(self):
        anchor = '2026-11-08T20:00:00+00:00'
        start, end = guard.reference_week(anchor, dt.datetime.fromisoformat('2026-11-14T20:59:00+00:00'))
        self.assertEqual((str(start), str(end)), ('2026-11-08', '2026-11-14'))
        start, end = guard.reference_week(anchor, dt.datetime.fromisoformat('2026-11-14T21:00:00+00:00'))
        self.assertEqual((str(start), str(end)), ('2026-11-15', '2026-11-21'))

    def test_acc1_source_and_policy_unchanged(self):
        before = json.loads((REPORT / 'limits_from_acc1/acc1_unchanged_before.json').read_text())
        for file, digest in before.items():
            self.assertEqual(hashlib.sha256((ROOT / file).read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main(verbosity=2)

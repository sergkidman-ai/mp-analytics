# поток: print
import unittest

from fastapi.testclient import TestClient

from web.print_app import create_app


class PrintWebTests(unittest.TestCase):
    def test_standalone_host_exposes_only_printing_without_other_pult_links(self):
        class SavedStatus:
            calls = 0

            def status(self):
                self.calls += 1
                return {'installed': True, 'rows': []}

        service = SavedStatus()
        with TestClient(create_app(lambda: service),headers={'X-Pult-User':'print'}) as client:
            page = client.get('/warehouse/print')
            self.assertEqual(page.status_code, 200)
            for path in ('/suppliers', '/reports', '/warehouse/customer-orders'):
                self.assertNotIn('href="' + path + '"', page.text)
            self.assertEqual(service.calls, 0)
            self.assertEqual(client.get('/api/print').json()['rows'], [])
            self.assertEqual(service.calls, 1)
            for path in ('/api/stock', '/suppliers', '/warehouse', '/docs', '/openapi.json'):
                self.assertEqual(client.get(path).status_code, 404)

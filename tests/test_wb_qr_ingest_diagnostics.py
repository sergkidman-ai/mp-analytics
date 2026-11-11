# поток: ret
"""Rejected client requests must be diagnosable without logging secrets or QR."""
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from returns_bot import wb_qr_ingest as ingest

class IngestDiagnosticsTests(unittest.TestCase):
    def test_rejections_log_only_safe_reason_and_scope(self):
        values={'wb_acc2':'isolated-diskver-secret','wb_acc1':'isolated-digital-secret'}
        with patch.object(ingest,'token',side_effect=lambda a='wb_acc2':values[a]):
            client=TestClient(ingest.app)
            with self.assertLogs('uvicorn.error',level='INFO') as logs:
                wrong=client.post('/api/returns/wb-qr/diskver',headers={'Authorization':'Bearer '+values['wb_acc1']},json={'png_b64':'PRIVATE_QR_PAYLOAD'})
                missing=client.post('/api/returns/wb-qr/diskver',json={'png_b64':'PRIVATE_QR_PAYLOAD'})
                invalid=client.post('/api/returns/wb-qr/diskver',headers={'Authorization':'Bearer '+values['wb_acc2']},json={})
                origin=client.options('/api/returns/wb-qr/diskver',headers={'Origin':'https://private-origin.invalid'})
            self.assertEqual([r.status_code for r in [wrong,missing,invalid,origin]],[401,401,400,403])
            text='\n'.join(logs.output)
            for word in ['wrong_company_token','token_missing','KeyError','reason=origin','company=diskver','status=401','status=400']:
                self.assertIn(word,text)
            for secret in [*values.values(),'PRIVATE_QR_PAYLOAD','Authorization','private-origin.invalid']:
                self.assertNotIn(secret,text)

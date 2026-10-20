# поток: fin
import unittest
from unittest.mock import patch
from collectors import ozon_accrual as C
from core.ozon_posting import PostingProtocolError

class AccrualTests(unittest.TestCase):
    def fetch(self):return C.fetch_day('oz_acc2','2026-10-20',limit=20,max_pages=2)
    def row(self,i):return {'accrual_id':i,'date':'2026-10-20'}
    def test_pages_pass_exact_cursor(self):
        with patch('collectors.ozon_postings.request_page',side_effect=[{'accruals':[self.row(1)],'last_id':'abc='},{'accruals':[self.row(2)],'last_id':'0'}]) as req:
            self.assertEqual(len(self.fetch()),2);self.assertEqual(req.call_args.args[2]['last_id'],'abc=')
    def test_repeat_cursor_fails_instead_of_partial_success(self):
        with patch('collectors.ozon_postings.request_page',side_effect=[{'accruals':[self.row(1)],'last_id':'a'},{'accruals':[self.row(2)],'last_id':'a'}]),self.assertRaises(PostingProtocolError):self.fetch()
    def test_invalid_schema_is_not_empty_success(self):
        for obj in ({},{'accruals':[]}, {'accruals':[{'accrual_id':1,'date':'2026-10-19'}],'last_id':'0'}):
            with patch('collectors.ozon_postings.request_page',return_value=obj),self.assertRaises(PostingProtocolError):self.fetch()
    def test_source_failure_before_load(self):
        with patch('collectors.ozon_postings.request_page',side_effect=TimeoutError()),patch.object(C,'load_raw') as load,self.assertRaises(TimeoutError):C.main('2026-10-20','2026-10-20','oz_acc2')
        load.assert_not_called()
    def test_empty_terminal_day(self):
        with patch('collectors.ozon_postings.request_page',return_value={'accruals':[],'last_id':''}):self.assertEqual(self.fetch(),[])

if __name__=='__main__':unittest.main()

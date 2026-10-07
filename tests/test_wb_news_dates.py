# поток: ev — regression for WB UTC dates on Python 3.10
import datetime as dt
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from ops import mp_news


class WbNewsDatesTest(unittest.TestCase):
    def collect(self, date):
        news={'id':1,'date':date,'header':'Изменение тарифов','content':'Новый тариф','types':[]}
        with patch.object(mp_news,'wb_news',return_value=[news]), \
             patch.object(mp_news.db,'query',return_value=[]), \
             patch.object(mp_news.db,'execute') as execute:
            out=mp_news.collect_wb(dry=False,days=44)
        return out,execute

    def test_z_timestamp_is_saved_with_utc(self):
        out,execute=self.collect('2026-09-28T13:17:26Z')
        self.assertEqual(len(out),1)
        self.assertEqual(out[0]['created_at'].utcoffset(),dt.timedelta(0))
        self.assertEqual(out[0]['created_at'].hour,13)
        execute.assert_called_once()

    def test_existing_offset_still_works(self):
        out,_=self.collect('2026-09-28T16:17:26+03:00')
        self.assertEqual(out[0]['created_at'].utcoffset(),dt.timedelta(hours=3))

    def test_bad_date_is_logged_and_not_saved(self):
        for value in ('broken',None):
            with self.subTest(value=value),redirect_stdout(io.StringIO()) as log:
                out,execute=self.collect(value)
            self.assertEqual(out,[])
            execute.assert_not_called()
            self.assertIn('некорректная дата',log.getvalue())

    def test_existing_news_is_not_inserted_again(self):
        news={'id':1,'date':'2026-09-28T13:17:26Z'}
        with patch.object(mp_news,'wb_news',return_value=[news]), \
             patch.object(mp_news.db,'query',return_value=[{'message_id':'1'}]), \
             patch.object(mp_news.db,'execute') as execute:
            self.assertEqual(mp_news.collect_wb(dry=False),[])
            execute.assert_not_called()


if __name__=='__main__':
    unittest.main()

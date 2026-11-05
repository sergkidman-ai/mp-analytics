# поток: rev
import ast
import datetime as dt
from pathlib import Path
from types import SimpleNamespace
import unittest

from collectors.yandex_feedback_coverage import question_jobs,review_jobs


class HistoryCoverage(unittest.TestCase):
    def test_open_old_question_keeps_its_month_in_regular_collection(self):
        now=dt.datetime(2026,11,4,tzinfo=dt.timezone.utc)
        jobs=question_jobs(now,[dt.datetime(2022,3,12,tzinfo=dt.timezone.utc)])
        self.assertEqual(jobs,[{'dateFrom':'2022-03-01','dateTo':'2022-03-31'},
                               {'dateFrom':'2026-10-01','dateTo':'2026-10-31'},
                               {'dateFrom':'2026-11-01','dateTo':'2026-11-04'}])

    def test_month_boundaries_cover_leap_day_without_future_dates(self):
        jobs=question_jobs(dt.datetime(2024,3,2,tzinfo=dt.timezone.utc))
        self.assertEqual(jobs[0],{'dateFrom':'2024-02-01','dateTo':'2024-02-29'})
        self.assertEqual(jobs[-1]['dateTo'],'2024-03-02')

    def test_older_reviews_are_checked_by_id_outside_implicit_six_month_window(self):
        now=dt.datetime(2026,11,4,tzinfo=dt.timezone.utc)
        ids=list(range(1,46))+[1]
        jobs=review_jobs(now,ids)
        self.assertIn('dateTimeFrom',jobs[0]);self.assertIn('dateTimeTo',jobs[0])
        self.assertEqual([len(j['feedbackIds']) for j in jobs[1:]],[20,20,5])
        self.assertEqual([x for job in jobs[1:] for x in job['feedbackIds']],list(range(1,46)))
        self.assertTrue(all(set(job)=={'feedbackIds'} for job in jobs[1:]))

    def collector(self,responses,cap=5):
        path=Path(__file__).resolve().parents[1]/'collectors/feedback_recovery.py'
        tree=ast.parse(path.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_collect_yandex_job')
        calls=[]
        def read(*args,**kwargs):
            calls.append(kwargs)
            return responses.pop(0)
        ns={'MAX_PAGES':cap,'PAGE_SIZE':20,'yq':SimpleNamespace(API='https://test.local'),
            'read':read,'yf':SimpleNamespace(_text_parts=lambda value:('','','')),
            'yandex_review_answer':lambda *a:(None,[]),'Json':lambda value:value,
            'persist':lambda rows:None,'db':SimpleNamespace(execute=lambda *a:None),
            'print':lambda *a,**k:None,'json':__import__('json'),'time':SimpleNamespace(sleep=lambda value:None)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),ns)
        return ns['_collect_yandex_job'],calls

    def first_page(self):
        return {'result':{'feedbacks':[{'feedbackId':1,'identifiers':{'offerId':'s'},'statistics':{'commentsCount':0}}],
                           'paging':{'nextPageToken':'second'}}}

    def test_actual_collector_passes_explicit_period_and_canonical_page_token(self):
        collect,calls=self.collector([self.first_page(),{'result':{'feedbacks':[]}}])
        body={'dateTimeFrom':'2026-01-01T00:00:00Z','dateTimeTo':'2026-04-01T00:00:00Z'}
        collect('review','test','business',{},set(),body)
        self.assertEqual(calls[0]['json'],body)
        self.assertEqual(calls[1]['params']['pageToken'],'second')
        self.assertNotIn('page_token',calls[1]['params'])

    def test_page_limit_is_reported_as_incomplete(self):
        collect,_=self.collector([self.first_page()],cap=1)
        with self.assertRaisesRegex(RuntimeError,'YandexPaginationIncomplete'):
            collect('review','test','business',{},set(),{})


if __name__=='__main__':unittest.main()

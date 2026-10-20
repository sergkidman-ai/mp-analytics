# поток: fin
"""Self-contained Ozon adapter contract; no live requests or production DB writes."""
import ast
import copy
import io
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from collectors import ozon_postings as api
from core.ozon_posting import PostingProtocolError, product_price, rub, posting_money_sql

def posting(n='1-1-1', scheme='fbs'):
    return {'posting_number': n, 'status': 'delivered',
            'in_process_at': '2026-10-20T10:00:00Z',
            'products': [{'offer_id': 'A', 'sku': 1, 'quantity': 3,
                          'price': {'amount': '100.01', 'currency': 'RUB'}}],
            'financial_data': {'products': [{'product_id': 1, 'price': 100.01,
                'payout': 200, 'customer_price': {'amount': '70.01', 'currency': 'RUB'}}]}}

def page(posts=None, more=False, cursor=''):
    return {'postings': posts or [], 'has_next': more, 'cursor': cursor}

def response(status=200, obj=None, retry=None):
    r=Mock(status_code=status, headers={} if retry is None else {'Retry-After':retry})
    r.json.return_value=page() if obj is None else obj
    if status >= 400:r.raise_for_status.side_effect=requests.HTTPError(str(status))
    return r

class PostingTests(unittest.TestCase):
    def setUp(self):
        for target, kwargs in [('requests.post',{}),('_headers',{'return_value':{}}),('time.sleep',{}),('account_lock',{})]:
            p=patch.object(api, target, **kwargs) if '.' not in target else patch('collectors.ozon_postings.'+target,**kwargs)
            setattr(self,target.replace('.','_'),p.start());self.addCleanup(p.stop)
        self.output=redirect_stdout(io.StringIO());self.output.__enter__();self.addCleanup(self.output.__exit__,None,None,None)

    def fetch(self, scheme='fbs'):
        return api.fetch_postings('oz_acc2',scheme,'2026-10-20','2026-10-20',limit=2)

    def test_pilot_date_interface_budget_and_page_cap(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        posts=api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',
                                limit=20,max_pages=10,window_budget=120)
        self.assertEqual(len(posts),1)
        self.account_lock.assert_called_once_with('oz_acc2',timeout=5)
        self.account_lock.return_value.__enter__.return_value.check.assert_called_once()

    def test_window_budget_alias_and_conflict_validation(self):
        self.requests_post.return_value=response()
        api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',timeout=120,window_budget=120)
        for kwargs in ({'window_budget':0},{'window_budget':True},{'window_budget':float('inf')},
                       {'timeout':60,'window_budget':120},{'lock_timeout':-1}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',**kwargs)

    def test_busy_or_lost_account_lock_never_calls_ozon(self):
        from core.ozon_posting_lock import OzonLockBusy,OzonLockLost
        self.account_lock.return_value.__enter__.side_effect=OzonLockBusy('busy')
        with self.assertRaises(OzonLockBusy):self.fetch()
        self.requests_post.assert_not_called()
        self.account_lock.return_value.__enter__.side_effect=None
        self.account_lock.return_value.__enter__.return_value.check.side_effect=OzonLockLost('lost')
        with self.assertRaises(OzonLockLost):self.fetch()
        self.requests_post.assert_not_called()

    def test_lock_rechecked_before_retry_and_held_between_pages(self):
        from core.ozon_posting_lock import OzonLockLost
        lease=self.account_lock.return_value.__enter__.return_value
        lease.check.side_effect=[None,OzonLockLost('lost')]
        self.requests_post.return_value=response(429,retry='0')
        with self.assertRaises(OzonLockLost):self.fetch()
        self.assertEqual(self.requests_post.call_count,1)
        self.account_lock.return_value.__exit__.assert_called_once()

    def test_lock_check_time_is_included_before_http_attempt(self):
        clock=[100]
        def check():clock[0]=105
        self.account_lock.return_value.__enter__.return_value.check.side_effect=check
        with patch.object(api.time,'monotonic',side_effect=lambda:clock[0]),self.assertRaises(TimeoutError):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',window_budget=4)
        self.requests_post.assert_not_called()

    def test_one_account_lease_is_held_for_all_pages(self):
        cm=self.account_lock.return_value
        pages=iter([page([posting()],True,'a'),page([posting('1-2-1')])])
        def request(*args,**kwargs):
            cm.__exit__.assert_not_called()
            return response(obj=next(pages))
        self.requests_post.side_effect=request
        self.assertEqual(len(self.fetch()),2)
        self.account_lock.assert_called_once();cm.__exit__.assert_called_once()
        self.assertEqual(cm.__enter__.return_value.check.call_count,2)

    def test_precise_aware_utc_window_is_not_expanded_to_a_day(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        start=datetime(2026,10,20,9,0,0,123456,tzinfo=timezone.utc)
        end=datetime(2026,10,20,10,1,tzinfo=timezone.utc)
        posts=api.fetch_postings('oz_acc2','fbs',start,end,limit=20,max_pages=2,timeout=30)
        self.assertEqual(len(posts),1)
        body=self.requests_post.call_args.kwargs['json']
        self.assertEqual(body['filter'],{'since':'2026-10-20T09:00:00.123456Z','to':'2026-10-20T10:01:00Z'})
        self.assertEqual(body['limit'],20)

    def test_offset_timestamps_normalize_to_utc(self):
        p=posting();p['in_process_at']='2026-10-20T08:00:00Z'
        self.requests_post.return_value=response(obj=page([p]))
        api.fetch_postings('oz_acc2','fbs','2026-10-20T10:00:00+03:00',
                           '2026-10-20T12:00:00+03:00',limit=20,max_pages=1,timeout=30)
        self.assertEqual(self.requests_post.call_args.kwargs['json']['filter'],
                         {'since':'2026-10-20T07:00:00Z','to':'2026-10-20T09:00:00Z'})

    def test_date_objects_keep_full_inclusive_legacy_days(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        api.fetch_postings('oz_acc2','fbs',date(2026,10,20),date(2026,10,20))
        body=self.requests_post.call_args.kwargs['json']
        self.assertEqual(datetime.fromisoformat(body['filter']['since'].replace('Z','+00:00')),
                         datetime(2026,10,20,tzinfo=timezone.utc))
        self.assertEqual(datetime.fromisoformat(body['filter']['to'].replace('Z','+00:00')),
                         datetime(2026,10,20,23,59,59,999000,tzinfo=timezone.utc))

    def test_naive_timestamps_and_reversed_precise_window_fail_before_http(self):
        for start,end in [('2026-10-20T09:00:00','2026-10-20T11:00:00Z'),
                          (datetime(2026,10,20,9),'2026-10-20T11:00:00Z'),
                          ('2026-10-20T11:00:00Z','2026-10-20T09:00:00Z')]:
            with self.subTest(start=start),self.assertRaises(ValueError):
                api.fetch_postings('oz_acc2','fbs',start,end)
        self.requests_post.assert_not_called()

    def test_outside_precise_window_fails_even_inside_same_day(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        with self.assertRaises(PostingProtocolError):
            api.fetch_postings('oz_acc2','fbs','2026-10-20T09:00:00Z','2026-10-20T09:30:00Z',limit=20,max_pages=1)

    def test_invalid_bounds_options_do_not_make_requests(self):
        options=[{'limit':True},{'limit':20.0},{'max_pages':0},{'max_pages':True},
                 {'max_pages':1.5},{'max_pages':api.MAX_PAGES+1},{'timeout':True},
                 {'timeout':0},{'timeout':float('inf')},{'timeout':float('nan')},
                 {'timeout':'30'},{'statuses':'awaiting_packaging'},{'statuses':[]},
                 {'statuses':[None]},{'sort_dir':'desc'}]
        for kwargs in options:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',**kwargs)
        self.requests_post.assert_not_called()

    def test_explicit_page_cap_never_returns_partial_window(self):
        self.requests_post.side_effect=[response(obj=page([posting()],True,'a')),
                                       response(obj=page([posting('1-2-1')],True,'b'))]
        with self.assertRaisesRegex(PostingProtocolError,'page limit'):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',max_pages=2)
        self.assertEqual(self.requests_post.call_count,2)
        self.time_sleep.assert_called_once_with(0.3)

    def test_explicit_window_timeout_is_forwarded_and_rejects_late_result(self):
        with patch.object(api.time,'monotonic',side_effect=[100,100,108]), \
             patch.object(api,'request_page',return_value=page([posting()])) as req, \
             self.assertRaises(TimeoutError):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',timeout=7)
        self.assertEqual(req.call_args.kwargs['deadline'],107)

    def test_window_timeout_after_page_one_never_requests_page_two(self):
        with patch.object(api.time,'monotonic',side_effect=[100,100,101,101,108]), \
             patch.object(api,'request_page',return_value=page([posting()],True,'a')) as req, \
             self.assertRaises(TimeoutError):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',timeout=7)
        req.assert_called_once()

    def test_retry_after_respects_explicit_total_timeout(self):
        self.requests_post.return_value=response(429,retry='11')
        with self.assertRaises(TimeoutError):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',timeout=10)
        self.time_sleep.assert_not_called()
        self.assertEqual(self.requests_post.call_count,1)

    def test_verified_fbs_filters_and_desc_across_pages(self):
        posts=[posting(f'1-{i}-1') for i in range(3)]
        for p,minute,status in zip(posts,(2,1,0),('awaiting_packaging','awaiting_deliver','awaiting_packaging')):
            p['in_process_at']=f'2026-10-20T10:0{minute}:00Z';p['status']=status
        self.requests_post.side_effect=[response(obj=page(posts[:2],True,'a')),response(obj=page(posts[2:]))]
        expected=['awaiting_packaging','awaiting_deliver']
        got=api.fetch_postings('oz_acc2','fbs','2026-10-20T09:00:00Z','2026-10-20T11:00:00Z',
                             limit=2,max_pages=2,timeout=30,statuses=expected,sort_dir='DESC')
        self.assertEqual(got,posts)
        for call in self.requests_post.call_args_list:
            body=call.kwargs['json'];self.assertEqual(body['filter']['statuses'],expected)
            self.assertEqual(body['sort_dir'],'DESC');self.assertNotIn('status',body['filter'])

    def test_server_ignoring_status_filter_fails(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        with self.assertRaisesRegex(PostingProtocolError,'status filter'):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',statuses=['awaiting_packaging'])

    def test_server_desc_violation_across_page_boundary_fails(self):
        p=posting();p['in_process_at']='2026-10-20T09:00:00Z'
        self.requests_post.side_effect=[response(obj=page([p],True,'a')),response(obj=page([posting('1-2-1')]))]
        with self.assertRaisesRegex(PostingProtocolError,'DESC order'):
            api.fetch_postings('oz_acc2','fbs','2026-10-20','2026-10-20',sort_dir='DESC')

    def test_fbo_does_not_guess_unverified_filters(self):
        for kwargs in ({'statuses':['awaiting_packaging']},{'sort_dir':'DESC'}):
            with self.assertRaises(ValueError):
                api.fetch_postings('oz_acc2','fbo','2026-10-20','2026-10-20',**kwargs)
        self.requests_post.assert_not_called()

    def test_pages_exact_cursor_fixed_window_and_all_statuses(self):
        p1,p2=posting(),posting('1-2-1');p2['status']='cancelled'
        self.requests_post.side_effect=[response(obj=page([p1],True,'opaque==')),response(obj=page([p2]))]
        self.assertEqual(self.fetch(),[p1,p2])
        calls=self.requests_post.call_args_list;bodies=[c.kwargs['json'] for c in calls]
        self.assertEqual([b['cursor'] for b in bodies],['','opaque=='])
        self.assertEqual(bodies[0]['filter'],bodies[1]['filter'])
        self.assertNotIn('status',bodies[0]['filter']);self.assertNotIn('sort_dir',bodies[0])
        self.assertNotIn('offset',bodies[0]);self.assertTrue(calls[0].args[0].endswith('/v4/posting/fbs/list'))

    def test_empty_terminal_window(self):
        self.requests_post.return_value=response();self.assertEqual(self.fetch(),[])

    def test_separate_fbo_contract(self):
        self.requests_post.return_value=response(obj=page([posting()]))
        self.assertEqual(len(self.fetch('fbo')),1)
        self.assertTrue(self.requests_post.call_args.args[0].endswith('/v3/posting/fbo/list'))

    def test_schema_failures_are_not_empty_success(self):
        for obj in ({},{'result':[]},[],{'postings':[],'has_next':False},page(more='false'),{'postings':{},'has_next':False,'cursor':''}):
            with self.subTest(obj=obj),self.assertRaises(PostingProtocolError):
                self.requests_post.return_value=response(obj=obj);self.fetch()

    def test_cursor_repeat_and_cycle(self):
        for cursors in (['c','c'],['a','b','a']):
            self.requests_post.side_effect=[response(obj=page([posting(f'1-{n}-1')],True,c)) for n,c in enumerate(cursors)]
            with self.assertRaises(PostingProtocolError):self.fetch()

    def test_duplicate_postings(self):
        self.requests_post.side_effect=[response(obj=page([posting()],True,'c')),response(obj=page([posting()]))]
        with self.assertRaises(PostingProtocolError):self.fetch()

    def test_nonterminal_empty_page_rejected(self):
        self.requests_post.return_value=response(obj=page(more=True,cursor='c'))
        with self.assertRaises(PostingProtocolError):self.fetch()

    def test_terminal_empty_page_after_full_page_is_valid(self):
        self.requests_post.side_effect=[response(obj=page([posting()],True,'c')),response()]
        self.assertEqual(len(self.fetch()),1)

    def test_missing_identity_bad_quantity_price_and_outside_window(self):
        changes=[('posting_number',''),('in_process_at','2026-10-19T12:00:00Z'),('in_process_at',None),('status',None),('products',[])]
        for key,value in changes:
            p=posting();p[key]=value;self.requests_post.return_value=response(obj=page([p]))
            with self.subTest(key=key,value=value),self.assertRaises(PostingProtocolError):self.fetch()
        for qty in (0,-1,True,'3'):
            p=posting();p['products'][0]['quantity']=qty;self.requests_post.return_value=response(obj=page([p]))
            with self.assertRaises(PostingProtocolError):self.fetch()
        p=posting();p['products'][0]['price']='100';self.requests_post.return_value=response(obj=page([p]))
        with self.assertRaises(PostingProtocolError):self.fetch()

    def test_page_count_cap(self):
        self.requests_post.return_value=response(obj=page([posting()],True,'c'))
        with patch.object(api,'MAX_PAGES',1),self.assertRaises(PostingProtocolError):self.fetch()

    def test_transient_429_zero_and_5xx_retry_backoff(self):
        self.requests_post.side_effect=[response(429,retry='0'),response(503),response()]
        self.assertEqual(self.fetch(),[])
        self.assertEqual([c.args[0] for c in self.time_sleep.call_args_list],[2,4])

    def test_retry_after_respected(self):
        self.requests_post.side_effect=[response(429,retry='10'),response()];self.fetch()
        self.time_sleep.assert_called_once_with(10)

    def test_retry_after_http_date_and_invalid_header(self):
        self.assertEqual(api.retry_after('nonsense'),0)
        self.assertEqual(api.retry_after('Thu, 01 Jan 1970 00:00:00 GMT'),0)
        self.assertEqual(api.retry_after('-1'),0)

    def test_attempt_limit_even_if_clock_does_not_advance(self):
        self.requests_post.return_value=response(429,retry='0')
        with self.assertRaisesRegex(RuntimeError,'retries exhausted'):self.fetch()
        self.assertEqual(self.requests_post.call_count,api.MAX_ATTEMPTS)
        self.assertEqual(self.time_sleep.call_count,api.MAX_ATTEMPTS-1)

    def test_elapsed_limit_does_not_sleep_or_ignore_server_pause(self):
        self.requests_post.return_value=response(429,retry='500')
        with self.assertRaises(TimeoutError):self.fetch()
        self.time_sleep.assert_not_called();self.assertEqual(self.requests_post.call_count,1)

    def test_transport_failure_is_bounded(self):
        self.requests_post.side_effect=requests.Timeout()
        with self.assertRaises(RuntimeError):self.fetch()
        self.assertEqual(self.requests_post.call_count,api.MAX_ATTEMPTS)

    def test_nonretryable_http_and_invalid_json(self):
        self.requests_post.return_value=response(403)
        with self.assertRaises(requests.HTTPError):self.fetch()
        self.assertEqual(self.requests_post.call_count,1)
        self.requests_post.return_value=response();self.requests_post.return_value.json.side_effect=ValueError()
        with self.assertRaises(PostingProtocolError):self.fetch()

    def test_second_page_failure_has_no_raw_write(self):
        self.requests_post.side_effect=[response(obj=page([posting()],True,'c')),response(400)]
        with patch.object(api.db,'upsert') as save,self.assertRaises(requests.HTTPError):api.main('2026-10-20','2026-10-20','oz_acc2')
        save.assert_not_called()

    def test_fbo_failure_has_no_fbs_partial_commit(self):
        self.requests_post.side_effect=[response(obj=page([posting()])),response(400)]
        with patch.object(api.db,'upsert') as save,self.assertRaises(requests.HTTPError):api.main('2026-10-20','2026-10-20','oz_acc2')
        save.assert_not_called()

    def test_both_schemes_commit_once_and_payload_is_unchanged(self):
        p=posting();original=copy.deepcopy(p)
        self.requests_post.side_effect=[response(obj=page([p])),response()]
        with patch.object(api.db,'upsert',return_value=1) as save:api.main('2026-10-20','2026-10-20','oz_acc2')
        save.assert_called_once();rec=save.call_args.args[1][0]
        self.assertEqual(rec['payload'].adapted,original);self.assertEqual(p,original)

    def test_foreign_customer_price_is_preserved_with_rub_seller(self):
        p=posting();p['financial_data']['products'][0]['customer_price']['currency']='KZT'
        self.requests_post.return_value=response(obj=page([p]))
        self.assertEqual(self.fetch()[0]['financial_data'],p['financial_data'])

    def test_money_currency_is_mandatory_even_for_customer(self):
        p=posting();del p['financial_data']['products'][0]['customer_price']['currency']
        self.requests_post.return_value=response(obj=page([p]))
        with self.assertRaises(PostingProtocolError):self.fetch()

    def test_missing_remaining_budget_never_requests_again(self):
        with patch.object(api.time,'monotonic',side_effect=[0,121]),self.assertRaises(TimeoutError):
            api.request_page(api.FBS_URL,{}, {})
        self.requests_post.assert_not_called()

    def test_run_daily_real_source_gate_preserves_margin_on_posting_failure(self):
        # Execute only the existing gate loop from the orchestrator's AST.
        # Never call/import main or collectors; the build is a spy.
        source=Path(api.BASE_DIR/'run_daily.py').read_text();tree=ast.parse(source)
        loop=next(n for n in ast.walk(tree) if isinstance(n,ast.For) and 'ozon_post_ok' in ast.unparse(n)
                  and 'failed_sources = []' in ast.unparse(n) and 'ozm.build' in ast.unparse(n)
                  and ast.unparse(n.target)=='(f, l)')
        spy=Mock();env={'months':[(date(2026,10,1),date(2026,10,31))],
            'ozon_txn_ok':{'2026-10-01':True},'ozon_post_ok':{'2026-10-01':False},
            'ozon_catalog_ok':True,'ozon_cogs_ok':True,'acc':'oz_acc2',
            'step':lambda name,fn:fn(),'ozm':Mock(build=spy)}
        exec(compile(ast.Module(body=[loop],type_ignores=[]),'<source gate>','exec'),env)
        spy.assert_not_called()
        env['ozon_post_ok']['2026-10-01']=True
        exec(compile(ast.Module(body=[loop],type_ignores=[]),'<source gate>','exec'),env)
        spy.assert_called_once_with('2026-10-01','2026-10-31','oz_acc2')

class MoneyTests(unittest.TestCase):

    def test_both_wire_schemas_decimal_and_quantity_once(self):
        for price in ('100.01', {'amount': '100.01', 'currency': 'RUB'}):
            p = {'price': price, 'currency_code': 'RUB', 'quantity': 3}
            self.assertEqual(product_price(p) * p['quantity'], Decimal('300.03'))

    def test_bad_currency_or_amount_not_treated_as_zero(self):
        for value in ({'amount': '1'}, {'amount': '1', 'currency': 'USD'}, {'amount': None, 'currency': 'RUB'}, {'amount': 'NaN', 'currency': 'RUB'}, {'amount': True, 'currency': 'RUB'}, {'amount': 'bad', 'currency': 'RUB'}):
            with self.subTest(value=value), self.assertRaises(PostingProtocolError):
                rub(value, legacy_currency='RUB')
        with self.assertRaises(PostingProtocolError):
            rub('1')
        with self.assertRaises(PostingProtocolError):
            product_price({'price': '1', 'currency_code': 'USD'})

    def test_sql_expansion_is_explicit_and_has_no_float_or_qty_multiplier(self):
        sql = posting_money_sql('SELECT RUB(fd.customer_price), RUB(pr.prod.price), FD_QTY(fd,p.payload)')
        self.assertNotIn('RUB(', sql)
        self.assertNotIn('FD_QTY(', sql)
        self.assertIn("->>'currency' = 'RUB'", sql)
        self.assertNotIn('::float', sql)
        self.assertIn("oz_product->>'sku' = fd->>'product_id'", sql)

if __name__=='__main__':unittest.main()

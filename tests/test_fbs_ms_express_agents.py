# поток: inv
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from ops import fbs_ms_import_next5 as importer, fbs_ms_source_scan as scanner
from ops.fbs_ms_order_payload import build_payload, Line, PLATFORM, EXPRESS_AGENT
from core.ms_api import ref


class Response:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


class ExpressTests(unittest.TestCase):
    def payload(self, platform, account, express):
        return build_payload(platform=platform, account=account, external_id='123',
            lines=[Line('product', Decimal(1), 10000)], expected_kopeks=10000,
            available=False, now=datetime.now(timezone.utc), is_express=express)

    def test_agents_for_both_platforms_and_ozon_companies(self):
        for platform, account in [('ozon','acc1'),('ozon','acc2'),('yandex','acc1')]:
            regular=self.payload(platform,account,False);express=self.payload(platform,account,True)
            self.assertEqual(regular['agent'],ref('counterparty',PLATFORM[platform][0]))
            self.assertEqual(express['agent'],ref('counterparty',EXPRESS_AGENT[platform]))
            self.assertEqual({k:v for k,v in regular.items() if k!='agent'},
                             {k:v for k,v in express.items() if k!='agent'})

    def test_unknown_platform_or_non_boolean_flag_rejected(self):
        for platform,flag in [('wb',True),('ozon','false')]:
            with self.assertRaises(ValueError):self.payload(platform,'acc1',flag)

    def test_ozon_uses_fresh_boolean_flag(self):
        for flag in [True,False,None]:
            post={'posting_number':'123-0001-1','status':'awaiting_packaging',
                'is_express':flag,'created_at':'2026-11-04T09:00:00+03:00',
                'products':[{'sku':1,'offer_id':'0001','quantity':1}],
                'financial_data':{'products':[{'product_id':1,'old_price':100}]}}
            with patch.object(importer.requests,'post',return_value=Response({'result':post})):
                got=importer.source({'platform':'ozon','account':'oz_acc1','number':post['posting_number']}, {})
            self.assertIs(got['is_express'],flag is True)

    def test_market_express_in_scan_and_fresh_source(self):
        order={'orderId':123,'campaignId':7,'programType':'EXPRESS','fake':False,
            'creationDate':'2026-11-04T09:00:00+03:00','status':'PROCESSING','substatus':'STARTED',
            'items':[{'offerId':'0001','count':1,'prices':{'payment':{'value':100,'currencyId':'RUR'}}}]}
        with patch.object(scanner.requests,'post',return_value=Response({'orders':[order]})) as request:
            rows=scanner.yandex(datetime(2026,11,4,20,tzinfo=timezone.utc))
        self.assertEqual(len(rows),1);self.assertTrue(rows[0]['is_express'])
        self.assertEqual(request.call_args.kwargs['json']['programTypes'],['FBS','EXPRESS'])
        with patch.object(importer.requests,'post',return_value=Response({'orders':[order]})):
            got=importer.source(rows[0],{})
        self.assertTrue(got['is_express'])

    def test_preflight_passes_fresh_mode_to_ms_payload(self):
        card={'id':'product','code':'0001wb','externalCode':'0001',
              'supplier':ref('counterparty','dd6f9218-ec82-11e6-7a69-97110027a1f0')}
        for platform in ['ozon','yandex']:
            source={'platform':platform,'account':'acc1','number':'123','is_express':True,
                'created_at':'2026-11-04T09:00:00+03:00','status':'awaiting_packaging',
                'lines':[{'market_code':'0001','quantity':1,'price_kopeks':10000,'discount_percent':'0'}]}
            with patch.object(importer,'source',return_value=source), \
                 patch.object(importer,'find_customerorder',return_value=None), \
                 patch.object(importer,'set_components',return_value=None), \
                 patch.object(importer,'product_cards',return_value=[card]), \
                 patch.object(importer,'_stock',return_value={'product':{'stock':10,'reserve':0}}):
                got=importer.inspect({'number':'123'}, {}, {'product':1000}, 'digest')
            self.assertEqual(got['payload']['agent'],ref('counterparty',EXPRESS_AGENT[platform]))

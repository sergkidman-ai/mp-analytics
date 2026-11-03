# поток: print
"""Loopback-only preview using saved, verified samples. NO external API calls.

Creates and removes its own print_preview_* schema. Never installs print_ops.
MS statuses/dates in this demo are local display fixtures, not live assertions.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

from fastapi import FastAPI
from psycopg2 import sql
import uvicorn

from core.db import get_conn
from print_service.clients import MS
from print_service.collector import Collector
from print_service.domain import MOSCOW, guard_sample_export
from print_service.queue import PrintQueue
from print_service.service import PrintService
from print_service.storage import atomic_bytes, save_json
from web.print_api import make_router


class OfflineSamples:
    lease_check = staticmethod(lambda: None)

    def __init__(self, source):
        self.source = Path(source)
        label_root = self.source / 'label-samples-20261103'
        self.entries, self.calls = {}, []
        stores = {}
        for raw in json.loads((self.source / 'today-collecting-ozon-read.json').read_text())['rows']:
            stores[raw['store']['meta']['href']] = raw['store'].get('name')
        for account in ('oz_acc1','wb_acc1','wb_acc2','ya_acc1'):
            folder = self.source / 'safe-pipeline-samples' / account
            original = json.loads((folder / 'source.json').read_text())
            order, demand = original['order'], original['demand']
            self.entries[account] = dict(order=order,demand=demand,
                positions=json.loads((folder/'positions.json').read_text()),receipt=(folder/'receipt_ms.pdf').read_bytes())
        ds = json.loads((label_root/'oz_acc2-receipt-export-retry-before.json').read_text())
        self.entries['oz_acc2'] = dict(order=ds['order'],demand=ds['demand'],
            positions=json.loads((label_root/'oz_acc2-today-positions.json').read_text()),
            receipt=(label_root/'oz_acc2-today-receipt-ms.pdf').read_bytes())
        for account, entry in self.entries.items():
            if isinstance(entry['positions'],dict):
                data=entry['positions']
                if data.get('meta',{}).get('size')!=len(data.get('rows',[])):
                    raise AssertionError('Incomplete saved shipment positions')
                entry['positions']=data['rows']
            order=entry['order']; order['store']['name']=stores.get(order['store']['meta']['href'], 'Склад из МС')
            if account.startswith('oz_'):
                entry['posting']=json.loads((label_root/(account+'-today-posting.json')).read_text())['result']
                entry['label']=(label_root/(account+'-today-label.pdf')).read_bytes()
            elif account.startswith('wb_'):
                entry['sticker']=json.loads((label_root/(account+'-stickers.json')).read_text())['stickers'][0]
                entry['label']=next(label_root.glob(account+'-*-label.pdf')).read_bytes()
            else:
                entry['market']=json.loads((self.source/'ya_acc1-63751218432-order-read.json').read_text())['orders'][0]
                entry['label']=(label_root/'ya_acc1-63751218432-label.pdf').read_bytes()
            assert entry['order']['name']==(entry.get('posting',{}).get('posting_number') or
                str(entry.get('sticker',{}).get('orderId') or entry.get('market',{}).get('orderId')))

    def _order(self, entry):
        raw=deepcopy(entry['order'])
        raw['state']={'name':'Собирается'}
        raw['deliveryPlannedMoment']=str(datetime.now(timezone.utc).astimezone(MOSCOW).date())+' 12:00:00'
        return raw

    def ms_get(self,path,params=None):
        self.calls.append(('ms_get',path))
        if path=='/entity/customerorder/metadata':
            return {'states':[{'name':'Собирается','meta':{'href':MS+'/entity/customerorder/metadata/states/00000000-0000-0000-0000-000000000001'}}]}
        if path=='/entity/customerorder':
            rows=[self._order(e) for e in self.entries.values() if e['order']['organization']['meta']['href'] in params['filter']]
            rows.sort(key=lambda r:r['id']);start=params['offset']
            return {'meta':{'size':len(rows)},'rows':rows[start:start+20]}
        for entry in self.entries.values():
            if path=='/entity/customerorder/'+entry['order']['id']:return self._order(entry)
            if path=='/entity/demand/'+entry['demand']['id']:return deepcopy(entry['demand'])
            if path=='/entity/demand/'+entry['demand']['id']+'/positions':
                rows=entry['positions'];start=params['offset'];return {'meta':{'size':len(rows)},'rows':deepcopy(rows[start:start+20])}
        raise AssertionError('Unexpected local fixture read')

    def ozon_posting(self,account,number):
        self.calls.append(('ozon_posting',account))
        entry=self.entries[account];assert entry['order']['name']==number
        return deepcopy(entry['posting'])

    def ozon_label(self,account,posting,folder):
        self.calls.append(('ozon_label',account));atomic_bytes(folder/'label.pdf',self.entries[account]['label'])

    def wb_status(self,account,number):
        self.calls.append(('wb_status',account))
        assert self.entries[account]['order']['name']==number
        return {'id':int(number),'supplierStatus':'complete'}

    def wb_sticker(self,account,number,folder):
        self.calls.append(('wb_sticker',account));self.wb_status(account,number);entry=self.entries[account]
        atomic_bytes(folder/'label.pdf',entry['label']);save_json(folder/'label_source.json',entry['sticker'])
        return entry['sticker']

    def yandex_order(self,number):
        self.calls.append(('yandex_order','ya_acc1'))
        assert self.entries['ya_acc1']['order']['name']==number
        return deepcopy(self.entries['ya_acc1']['market'])

    def yandex_label(self,order,folder):
        self.calls.append(('yandex_label','ya_acc1'));atomic_bytes(folder/'label.pdf',self.entries['ya_acc1']['label'])

    def export_receipt(self,oid,did,folder,*,expected_account,expected_number,expected_day=None,allow_unprinted=False):
        entry=self.entries[expected_account]
        assert entry['order']['id']==oid and entry['demand']['id']==did and entry['order']['name']==expected_number
        guard_sample_export(entry['order'],entry['demand'],linked_order_id=oid)
        self.calls.append(('saved_receipt',expected_account))
        atomic_bytes(folder/'receipt_ms.pdf',entry['receipt'])
        return entry['receipt']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args();args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    queue=PrintQueue(schema='print_preview_'+uuid4().hex);queue.install()
    samples=OfflineSamples(args.source)
    service=PrintService(queue=queue,clients=samples,root=args.output/'cache',allow_unprinted=False,demo=True)
    stop=threading.Event()
    def background():
        while not stop.wait(.3):
            queue.heartbeat(service.clock())
            sid=queue.start_sync()
            if sid:service.run_sync(sid)
            batch=queue.pending_batch()
            if batch:service.run_batch(batch)
    try:
        queue.sync_snapshot(Collector(samples).collect(now=service.clock()),started_at=service.clock(),complete=True)
        queue.heartbeat(service.clock())
        # Demonstrate independent receipt/label readiness before a pair exists.
        entry=samples.entries['ya_acc1'];label=args.output/'cached_market_label.pdf';atomic_bytes(label,entry['label'])
        queue.mark_artifact(entry['order']['id'],demand_id=entry['demand']['id'],kind='label',path=label)
        app=FastAPI();app.include_router(make_router(lambda:service))
        @app.get('/__preview__/state')
        def preview_state():
            return {'saved_provider_calls':len(samples.calls),'external_api':False}
        worker=threading.Thread(target=background,daemon=True);worker.start()
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,log_level='warning'))
        def announce():
            while not server.started and not stop.wait(.1):pass
            if server.started:
                port=server.servers[0].sockets[0].getsockname()[1]
                save_json(args.output/'runtime.json',dict(schema=queue.schema,url=f'http://127.0.0.1:{port}',external_api=False))
                print(f'Local preview: http://127.0.0.1:{port}/warehouse/print',flush=True)
        threading.Thread(target=announce,daemon=True).start()
        server.run()
    finally:
        stop.set()
        if 'worker' in locals():worker.join(timeout=30)
        save_json(args.output/'offline_calls.json',samples.calls)
        assert queue.schema.startswith('print_preview_')
        with get_conn() as connection:
            with connection.cursor() as cursor:cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(queue.schema)))


if __name__=='__main__':main()

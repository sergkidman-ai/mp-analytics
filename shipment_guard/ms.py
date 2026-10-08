# поток: fin
"""Single worker owns the advisory lock. Every MS read uses this single serial limiter."""
import os
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import requests
from shipment_guard import config, store
from core import db

class SourceError(Exception):
    def __init__(self, code, retry_after=60):
        super().__init__(code)
        self.retry_after=retry_after

class NotFound(SourceError):
    pass

def retry_seconds(headers, now=None):
    value=headers.get('Retry-After')
    if value:
        try: return max(1,float(value))
        except ValueError:
            try: return max(1,(parsedate_to_datetime(value)-(now or datetime.now(timezone.utc))).total_seconds())
            except (ValueError,TypeError): pass
    try: return max(1,float(headers.get('X-Lognex-Retry-After',3000))/1000)
    except ValueError: return 60

class MSClient:
    def __init__(self, session=None, clock=time.monotonic, sleep=time.sleep):
        self.session=session or requests.Session()
        self.clock,self.sleep=clock,sleep
        self.next_at=0
        self.persistent=session is None
        self.interval=max(1,float(os.getenv('SHIPMENT_GUARD_MS_INTERVAL','1')))
        if self.persistent:
            rows=db.query("SELECT value FROM ms_shipment_metrics WHERE name='ms_not_before_epoch'")
            pause=max(self.interval,float(rows[0]['value'])-time.time()) if rows else self.interval
            self.next_at=self.clock()+pause
        self.lock=threading.Lock()
        self.last_start=None
        self.min_gap=None
        self.requests=0
        self.pulse=lambda: None

    def get(self, path, params=None):
        if not path.startswith(('/entity/demand','/report/stock/byoperation')):
            raise ValueError('MS resource forbidden')
        with self.lock:
            self.pulse()
            delay=self.next_at-self.clock()
            while delay>0:
                self.sleep(min(20,delay)); self.pulse(); delay=self.next_at-self.clock()
            start=self.clock()
            if self.last_start is not None:
                gap=start-self.last_start
                self.min_gap=min(self.min_gap,gap) if self.min_gap is not None else gap
            self.last_start=start
            self.next_at=start+self.interval
            self.requests+=1
            try:
                r=self.session.get(config.MS+path,params=params,
                  headers={'Authorization':'Bearer '+os.environ['MOYSKLAD_TOKEN'],'Accept-Encoding':'gzip'},timeout=(5,45))
            except requests.RequestException:
                store.metric('ms_network_errors')
                raise SourceError('ms_network_error') from None
            headers=r.headers
            remaining=headers.get('X-RateLimit-Remaining') or headers.get('X-Lognex-RateLimit-Remaining')
            try:
                if remaining is not None and float(remaining)<10:
                    self.next_at=max(self.next_at,self.clock()+max(5,retry_seconds(headers)))
            except ValueError: pass
            if self.persistent:
                store.metric('ms_not_before_epoch',time.time()+max(0,self.next_at-self.clock()),replace=True)
            store.metric('ms_requests',details={'last_status':r.status_code,'remaining':remaining,
                          'min_start_gap_seconds':self.min_gap,'duration_seconds':round(self.clock()-start,3)})
            if r.status_code==429:
                pause=retry_seconds(headers)
                self.next_at=max(self.next_at,self.clock()+pause)
                if self.persistent:
                    store.metric('ms_not_before_epoch',time.time()+pause,replace=True)
                store.metric('ms_429',details={'pause_seconds':pause})
                raise SourceError('ms_429',pause)
            if r.status_code==404: raise NotFound('ms_404')
            if r.status_code!=200: raise SourceError('ms_http_'+str(r.status_code))
            try: return r.json()
            except ValueError: raise SourceError('ms_invalid_json') from None

    def rows(self, path, params=None, limit=100):
        offset=0
        while True:
            j=self.get(path,{**(params or {}),'limit':limit,'offset':offset})
            rows=j.get('rows')
            if not isinstance(rows,list): raise SourceError('ms_missing_rows')
            yield from rows
            offset+=len(rows)
            size=j.get('meta',{}).get('size')
            if size is not None:
                if offset>=size: return
                if not rows: raise SourceError('ms_incomplete_page')
            elif len(rows)<limit: return

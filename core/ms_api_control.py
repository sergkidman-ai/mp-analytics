# поток: infra
"""Shared local MS admission control. No credentials, query strings or payloads stored."""
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import fcntl
import json
import os
import re
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path('/opt/mp-analytics/runtime/ms_api_control')
WINDOW = 3.0
BUDGET = 30  # 45 provider units, leaving room for external integrations.
SLOTS = 3


def enabled():
    return os.getenv('MS_API_CONTROL', '1') != '0'


def priority():
    override = os.getenv('MS_API_PRIORITY')
    if override in ('operational', 'background'):
        return override
    command = ' '.join(sys.argv).lower()
    return ('operational' if any(x in command for x in (
        'customer_guard', 'shipment_guard', 'fbs_ms_', 'ms_supply', 'invoice_bot',
        'ms_market_status', 'order_priority')) else 'background')


def weight(url, now=None):
    path = urlsplit(url).path.rstrip('/')
    if path.endswith(('/report/stock/all', '/report/stock/bystore')):
        return 5
    day = (now or datetime.now(timezone.utc)).date()
    from datetime import date
    return 4 if day >= date(2026,12,1) else 3 if day >= date(2026,9,1) else 2


def resource(url):
    path = urlsplit(url).path.split('/api/remap/1.2',1)[-1]
    return re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27}', '{id}', path, flags=re.I)[:180]


def retry_seconds(headers, now=None):
    headers = {str(k).lower():v for k,v in (headers or {}).items()}
    values = []
    for key in ('x-lognex-retry-timeinterval','x-lognex-retry-after','x-lognex-reset'):
        try: values.append(max(0,float(headers.get(key,0))/1000))
        except (ValueError,TypeError): pass
    value = headers.get('retry-after')
    if value:
        try: values.append(max(0,float(value)))
        except (ValueError,TypeError):
            try: values.append(max(0,parsedate_to_datetime(value).timestamp()-(now or time.time())))
            except (ValueError,TypeError,OverflowError): pass
    return min(300,max(1.0,max(values or [1.0])))


@contextmanager
def state():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT/'state.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=ROOT/'state.json'
        try:
            value=json.loads(path.read_text())
            if not isinstance(value,dict):raise ValueError('invalid state')
        except FileNotFoundError:
            value={}
        except (OSError,ValueError):
            # Lost recent admissions: wait one full provider window.
            value={'not_before':time.time()+WINDOW}
        yield value
        temp=ROOT/('state.'+uuid.uuid4().hex+'.tmp')
        temp.write_text(json.dumps(value,separators=(',',':')))
        os.replace(temp,path)


def alive(pid):
    try:os.kill(int(pid),0);return True
    except (ProcessLookupError,ValueError):return False
    except PermissionError:return True


class Permit:
    def __init__(self, method, url, client, kind=None, timeout=60):
        self.method,self.url,self.client=method,url,client
        self.kind=kind or priority()
        self.timeout=timeout
        self.ticket=uuid.uuid4().hex
        self.handles=[]
        self.started=None
        self.waited=0

    def acquire(self):
        began=time.monotonic()
        deadline=began+self.timeout
        try:
            while True:
                delay=.05
                with state() as data:
                    now=time.time()
                    waiters={k:v for k,v in data.get('waiters',{}).items()
                             if v['expires']>now and alive(v['pid'])}
                    if self.kind=='operational':
                        waiters[self.ticket]={'pid':os.getpid(),'expires':now+max(1,deadline-time.monotonic())}
                    data['waiters']=waiters
                    starts=[row for row in data.get('starts',[]) if now-row[0]<WINDOW]
                    data['starts']=starts
                    blocked=data.get('not_before',0)>now
                    if blocked:delay=min(1,data['not_before']-now)
                    elif self.kind=='background' and waiters:delay=.1
                    elif sum(row[1] for row in starts)+weight(self.url)>BUDGET:
                        delay=max(.02,min(1,WINDOW-(now-starts[0][0])))
                    else:
                        handles=[]
                        try:
                            if self.kind=='background':
                                handle=(ROOT/'background.slot').open('a')
                                handles.append(handle);fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            for index in range(SLOTS):
                                handle=(ROOT/f'http{index}.slot').open('a')
                                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                                except BlockingIOError:handle.close();continue
                                handles.append(handle);break
                            else:raise BlockingIOError()
                        except BlockingIOError:
                            for handle in handles:handle.close()
                        else:
                            self.handles=handles
                            data['waiters'].pop(self.ticket,None)
                            starts.append([now,weight(self.url)])
                            self.started=time.monotonic();self.waited=self.started-began
                            return self
                if time.monotonic()>=deadline:
                    raise TimeoutError('MS shared request admission deadline exceeded')
                time.sleep(min(delay,max(0,deadline-time.monotonic())))
        except BaseException:
            self.release()
            raise

    def release(self):
        for handle in self.handles:handle.close()
        self.handles=[]
        with state() as data:
            data.get('waiters',{}).pop(self.ticket,None)

    def record(self, status, headers=None):
        with state() as data:
            now=time.time()
            if status==429:
                data['not_before']=max(data.get('not_before',0),now+retry_seconds(headers))
            lower={str(k).lower():v for k,v in (headers or {}).items()}
            try:
                if float(lower.get('x-ratelimit-remaining',999))<weight(self.url):
                    data['not_before']=max(data.get('not_before',0),now+retry_seconds(headers))
            except (ValueError,TypeError):pass
            date_key=datetime.now(timezone.utc).strftime('%Y-%m-%d')
            days=data.setdefault('metrics',{})
            daily=days.setdefault(date_key,{})
            key='|'.join((self.client,self.kind,self.method,resource(self.url),str(status)))
            metric=daily.setdefault(key,{'requests':0,'wait_seconds':0,'duration_seconds':0})
            metric['requests']+=1
            metric['wait_seconds']=round(metric['wait_seconds']+self.waited,3)
            metric['duration_seconds']=round(metric['duration_seconds']+time.monotonic()-self.started,3)
            for old in sorted(days)[:-3]:del days[old]


@contextmanager
def transport(method,url,opener,*,client='unknown',kind=None,timeout=60):
    """Hold a shared slot until caller has consumed/closed the HTTP response.

    Does not retry or change write semantics. Existing callers retain retry policy.
    """
    if not enabled() or urlsplit(url).hostname!='api.moysklad.ru':
        yield opener();return
    permit=Permit(method,url,client,kind,timeout).acquire()
    response=None;error=None
    try:
        response=opener()
        yield response
    except BaseException as exc:
        error=exc
        raise
    finally:
        status=getattr(response,'status_code',None) or getattr(response,'status',None) or getattr(error,'code',0)
        headers=getattr(response,'headers',None) or getattr(error,'headers',None)
        try:
            permit.record(int(status or 0),headers)
        except Exception:
            pass  # Telemetry failure must not turn a successful write into an error.
        finally:
            try:permit.release()
            except OSError:pass


@contextmanager
def urlopen(request,*,opener,timeout,client='urllib',kind=None):
    with transport(request.get_method(),request.full_url,
                   lambda:opener(request,timeout=timeout),client=client,kind=kind) as response:
        with response:
            yield response


def request(method,url,*,sender,client='requests',kind=None,**kwargs):
    # requests eagerly downloads content unless stream=True; streaming must use transport.
    if kwargs.get('stream'):
        raise ValueError('MS streaming requires an explicit transport context')
    with transport(method.upper(),url,lambda:sender(url,**kwargs),client=client,kind=kind) as response:
        return response

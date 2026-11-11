# поток: infra
import json
import multiprocessing as mp
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError
import pytest
from core import ms_api_control as control

URL='https://api.moysklad.ru/api/remap/1.2/entity/product/11111111-1111-1111-1111-111111111111?filter=SECRET'

@pytest.fixture(autouse=True)
def isolated(monkeypatch,tmp_path):
    monkeypatch.setattr(control,'ROOT',tmp_path)
    monkeypatch.setattr(control,'BUDGET',999)
    monkeypatch.setenv('MS_API_CONTROL','1')


def _process_read(kind,active,maximum,lock):
    def open_request():
        with lock:
            active.value+=1;maximum.value=max(maximum.value,active.value)
        time.sleep(.06)
        with lock:active.value-=1
        return SimpleNamespace(status_code=200,headers={})
    control.request('GET',URL,sender=lambda *a,**k:open_request(),client='test',kind=kind)

@pytest.mark.parametrize('kind,limit',[('operational',3),('background',1)])
def test_shared_limit_across_processes(kind,limit):
    context=mp.get_context('fork')
    active=context.Value('i',0);maximum=context.Value('i',0);lock=context.Lock()
    workers=[context.Process(target=_process_read,args=(kind,active,maximum,lock)) for _ in range(7)]
    for worker in workers:worker.start()
    for worker in workers:worker.join(5);assert worker.exitcode==0
    assert 1 <= maximum.value <= limit
    state=json.loads((control.ROOT/'state.json').read_text())
    assert sum(m['requests'] for day in state['metrics'].values() for m in day.values())==7
    assert 'SECRET' not in json.dumps(state) and '11111111' not in json.dumps(state)


def test_operational_waiter_has_priority_over_background():
    held=[control.Permit('GET',URL,'held','operational').acquire() for _ in range(3)]
    order=[];release_op=threading.Event()
    def run(kind):
        with control.transport('GET',URL,lambda:SimpleNamespace(status_code=200,headers={}),client='test',kind=kind):
            order.append(kind)
            if kind=='operational':release_op.wait(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        background=pool.submit(run,'background');time.sleep(.03)
        operational=pool.submit(run,'operational')
        for _ in range(50):
            with control.state() as state:
                if state.get('waiters'):break
            time.sleep(.01)
        held.pop().release()
        for _ in range(50):
            if order:break
            time.sleep(.01)
        assert order==['operational']
        release_op.set();operational.result(3);background.result(3)
    for permit in held:permit.release()


def test_weighted_budget_blocks_next_start(monkeypatch):
    monkeypatch.setattr(control,'BUDGET',6);monkeypatch.setattr(control,'WINDOW',.15)
    monkeypatch.setattr(control,'weight',lambda url:3)
    starts=[]
    for _ in range(3):
        with control.transport('GET',URL,lambda:SimpleNamespace(status_code=200,headers={}),client='test',kind='operational'):
            starts.append(time.monotonic())
    assert starts[2]-starts[0]>=.12


def test_429_cooldown_is_shared_and_no_retry_is_added(monkeypatch):
    calls=[];monkeypatch.setattr(control,'retry_seconds',lambda headers:.15)
    def fail():
        calls.append(1);raise HTTPError(URL,429,'limit',{},None)
    with pytest.raises(HTTPError):
        with control.transport('POST',URL,fail,client='first',kind='operational'):pass
    before=time.monotonic()
    with control.transport('GET',URL,lambda:SimpleNamespace(status_code=200,headers={}),client='other',kind='operational'):pass
    assert time.monotonic()-before>=.12
    assert calls==[1]


def test_successful_post_is_not_retried_or_failed_by_metrics(monkeypatch):
    calls=[]
    monkeypatch.setattr(control.Permit,'record',lambda *a,**k:(_ for _ in ()).throw(OSError('disk')))
    response=SimpleNamespace(status_code=201,headers={})
    assert control.request('POST',URL,sender=lambda *a,**k:calls.append(1) or response,kind='operational') is response
    assert calls==[1]


def test_timeout_happens_before_http_call():
    held=[control.Permit('GET',URL,'held','operational').acquire() for _ in range(3)]
    calls=[]
    try:
        with pytest.raises(TimeoutError):
            with control.transport('POST',URL,lambda:calls.append(1),kind='operational',timeout=.1):pass
        assert not calls
    finally:
        for permit in held:permit.release()


def _hold_until_killed(event):
    control.Permit('GET',URL,'child','operational').acquire()
    event.set();time.sleep(10)


def test_crashed_process_does_not_leak_a_slot():
    context=mp.get_context('fork');event=context.Event()
    worker=context.Process(target=_hold_until_killed,args=(event,));worker.start()
    assert event.wait(2);worker.terminate();worker.join(2)
    held=[control.Permit('GET',URL,'new','operational',timeout=.3).acquire() for _ in range(3)]
    for permit in held:permit.release()


def test_weight_and_retry_header_units():
    from datetime import datetime,timezone
    assert control.weight(URL,datetime(2026,11,9,tzinfo=timezone.utc))==3
    assert control.weight(URL,datetime(2026,12,1,tzinfo=timezone.utc))==4
    assert control.weight('https://api.moysklad.ru/api/remap/1.2/report/stock/all')==5
    assert control.retry_seconds({'X-Lognex-Retry-TimeInterval':'2500'})==2.5
    assert control.retry_seconds({'Retry-After':'4'})==4
    assert control.retry_seconds({})==1


def test_non_ms_requests_are_not_limited_or_recorded():
    assert control.request('GET','https://api-seller.ozon.ru/x',sender=lambda *a,**k:'ok')=='ok'
    assert not (control.ROOT/'state.json').exists()

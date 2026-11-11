# поток: fin
from unittest.mock import Mock
from returns_bot.sources import wb, yandex
from returns_bot import net


def test_wb_current_report_paginates_and_keeps_active_status(monkeypatch):
    monkeypatch.setattr(wb, '_token', lambda account: 'test')
    sleeps = []; monkeypatch.setattr(wb.time, 'sleep', sleeps.append)
    calls = []
    responses = [{'report': [{'shkId': n} for n in range(1, 1001)]},
                 {'report': [{'shkId': 1001, 'returnStatus': 'Выдано'}]},
                 {'report': [{'shkId': 1002, 'returnStatus': 'В пути в пвз'}]}]
    def request(method, url, **kw):
        calls.append((url, kw['params'])); return responses.pop(0)
    monkeypatch.setattr(wb, 'request_json', request)
    data = wb.fetch_raw('wb_acc1')
    assert len(data) == 1002
    assert [r[1]['offset'] for r in calls] == [0, 1000, 0]
    assert all(r[0].endswith('/api/analytics/v1/item-returns') for r in calls)
    assert sleeps == [60, 60]
    head, _ = wb.normalize('wb_acc1', data[-1])
    assert head['status_raw'] == 'В пути в пвз' and head['stage'] == 'transit'
    head, _ = wb.normalize('wb_acc1', data[-2])
    assert head['stage'] == 'closed'


def test_empty_report_204_does_not_decode_json(monkeypatch):
    response = Mock(status_code=204)
    monkeypatch.setattr(net.requests, 'request', lambda *a, **kw: response)
    assert net.request_json('GET', 'https://example.test') == {}
    response.json.assert_not_called()


def test_same_wb_barcode_can_have_two_distinct_return_journeys():
    a = dict(shkId=123, orderId=45, orderDt='2026-10-01', returnType='FBS', returnStatus='Выдано')
    b = dict(a, orderId=0, orderDt='2026-10-20', returnType='FBW', returnStatus='Неподбор')
    first = wb.normalize('wb_acc2', a)[0]['return_id']
    assert first != wb.normalize('wb_acc2', b)[0]['return_id']
    assert first == wb.normalize('wb_acc2', dict(a, returnStatus='В пути', completedDt='2026-11-05'))[0]['return_id']


def test_yandex_discovers_only_enabled_stores_of_our_business(monkeypatch):
    monkeypatch.setenv('YANDEX_CAMPAIGN_ID_ACC1', '')
    monkeypatch.setenv('YANDEX_BUSINESS_ID_ACC1', '123')
    monkeypatch.setattr(yandex, '_headers', lambda: {})
    def request(method, url, **kw):
        assert url.endswith('/v2/campaigns')
        return {'campaigns': [dict(id=1, business={'id': 123}, apiAvailability='AVAILABLE'),
            dict(id=2, business={'id': 123}, apiAvailability='MANUALLY_DISABLED'),
            dict(id=3, business={'id': 456}, apiAvailability='AVAILABLE')]}
    monkeypatch.setattr(yandex, 'request_json', request)
    assert yandex.campaigns() == ['1']


def test_yandex_current_pagination_parameter(monkeypatch):
    monkeypatch.setattr(yandex, '_headers', lambda: {})
    monkeypatch.setattr(yandex.time, 'sleep', lambda _: None)
    calls = []
    def request(method, url, **kw):
        assert url.endswith('/v2/campaigns/1/returns')
        calls.append(kw['params'])
        return {'result': {'returns': [{'id': len(calls)}],
            'paging': {'nextPageToken': 'next'} if len(calls) == 1 else {}}}
    monkeypatch.setattr(yandex, 'request_json', request)
    assert len(yandex.fetch_raw('1')) == 2
    assert calls[1]['pageToken'] == 'next'

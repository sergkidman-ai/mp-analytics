# поток: fin
import datetime as dt

from web import turnover_api
from tools import turnover_brands_refresh as brands


def test_direct_royalty_page_does_not_rewrite_javascript_selectors():
    html = turnover_api.royalty_page().body.decode()
    assert '<a href="/turnover/royalty" class="cur">' in html
    assert "document.querySelector('a[href=\"/turnover/royalty\"]')" in html
    assert 'a[href="/turnover/royalty" class=' not in html


def test_six_month_window_uses_calendar_months(monkeypatch):
    class Today(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 10)

    monkeypatch.setattr(turnover_api.dt, 'date', Today)
    seen = []

    def source(since):
        seen.append(since)
        return []

    for name in ['_wb', '_ozon', '_yandex']:
        monkeypatch.setattr(turnover_api, name, source)
    monkeypatch.setattr(turnover_api, '_order_counts', lambda since: {})
    monkeypatch.setattr(turnover_api.db, 'query', lambda *args: [{'d': None}])
    turnover_api._calculate_turnover(6)
    assert seen == [dt.date(2026, 6, 1)] * 3
    seen.clear()
    turnover_api._calculate_turnover(14)
    assert seen == [dt.date(2025, 10, 1)] * 3


def test_existing_attributes_need_no_api_requests(monkeypatch):
    monkeypatch.setattr(brands.db, 'query', lambda *args: [])
    monkeypatch.setattr(brands.requests, 'post', lambda *args, **kw: (_ for _ in ()).throw(AssertionError('unexpected API')))
    assert brands.refresh() == {'needed': 0, 'saved': 0, 'unavailable': 0, 'errors': 0}


def test_refresh_only_missing_offers_in_batches_of_twenty(monkeypatch):
    offers = [str(i) for i in range(43)]
    monkeypatch.setattr(brands.db, 'query', lambda *args: [dict(account='oz_acc1', offer_id=v) for v in offers])
    monkeypatch.setattr(brands, '_headers', lambda account: {})
    calls, saved = [], []

    def request(url, **kwargs):
        body = kwargs['json']
        assert body['limit'] == 20 and len(body['filter']['offer_id']) <= 20
        batch = body['filter']['offer_id']
        calls.append(batch)

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {'result': [dict(offer_id=v, attributes=[]) for v in batch]}

        return Response()

    monkeypatch.setattr(brands.requests, 'post', request)
    monkeypatch.setattr(brands, 'load_raw', lambda account, items: saved.extend(items))
    assert brands.refresh()['saved'] == 43
    assert list(map(len, calls)) == [20, 20, 3]
    assert [r['offer_id'] for r in saved] == offers


def test_wrong_product_identity_never_written(monkeypatch):
    monkeypatch.setattr(brands.db, 'query', lambda *args: [dict(account='oz_acc1', offer_id='wanted')])
    monkeypatch.setattr(brands, '_headers', lambda account: {})

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'result': [dict(offer_id='unrelated')]}

    monkeypatch.setattr(brands.requests, 'post', lambda *args, **kwargs: Response())
    monkeypatch.setattr(brands, 'load_raw', lambda *args: (_ for _ in ()).throw(AssertionError('unexpected write')))
    assert brands.refresh()['errors'] == 1

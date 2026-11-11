# поток: print
"""Read at most five EXACT MS order numbers and their linked demands.

Run from the worktree: python -B -m ops.print_preflight_read --output /tmp NUMBER ...
Creates a private evidence directory. Never exports PDFs or modifies documents.
No marketplace requests, status transitions, or automatic retries.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from dotenv import load_dotenv
import requests

from print_service.domain import ACCOUNTS, PrintBlocked, guard_sample_export
from shipment_guard.config import identity

BASE = 'https://api.moysklad.ru/api/remap/1.2'


def save(folder, name, data):
    fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def read_json(session, path, **params):
    response = session.get(BASE + path, params=params or None,
                           timeout=(5, 20), allow_redirects=False)
    if response.status_code != 200:
        raise PrintBlocked('ms_http_' + str(response.status_code))
    return response.json()


def read_order(session, number, folder):
    data = read_json(session, '/entity/customerorder', limit=20,
                     filter='name=' + number, expand='state,store,organization,agent')
    save(folder, number + '-search.json', data)
    all_rows = data.get('rows', [])
    exact = [r for r in all_rows if r.get('name') == number]
    if data.get('meta', {}).get('size') != len(all_rows) or len(exact) != 1:
        raise PrintBlocked('missing_ambiguous_or_incomplete_order_search')
    order = exact[0]
    platform, account, _, _ = identity(order)
    if account not in ACCOUNTS:
        raise PrintBlocked('unsupported_account')
    links = order.get('demands', [])
    if len(links) != 1:
        raise PrintBlocked('linked_demand_missing_or_ambiguous')
    href = links[0].get('meta', {}).get('href', '')
    demand_id = href.rsplit('/', 1)[-1]
    try:
        if str(UUID(demand_id)) != demand_id:
            raise ValueError()
    except (ValueError, TypeError):
        raise PrintBlocked('demand_uuid_invalid') from None
    if href != BASE + '/entity/demand/' + demand_id:
        raise PrintBlocked('demand_reference_invalid')
    demand = read_json(session, '/entity/demand/' + demand_id)
    save(folder, number + '-demand.json', demand)
    try:
        guard_sample_export(order, demand, linked_order_id=order['id'])
        sample_blocker = None
    except PrintBlocked as exc:
        sample_blocker = str(exc)
    return dict(number=number, platform=platform, account=account,
                ms_order_id=order['id'], ms_demand_id=demand_id,
                state=order.get('state', {}).get('name'),
                delivery_planned=order.get('deliveryPlannedMoment'),
                physical_store=order.get('store', {}).get('name'),
                order_printed=order.get('printed'), demand_printed=demand.get('printed'),
                sample_export_blocker=sample_blocker,
                **{'export_performed': False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('numbers', nargs='+')
    args = parser.parse_args()
    if not args.output.is_dir():
        parser.error('--output must be an existing directory')
    if (not 1 <= len(args.numbers) <= 5 or len(set(args.numbers)) != len(args.numbers)
            or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', n) for n in args.numbers)):
        parser.error('provide 1..5 distinct full order numbers')
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    token = os.getenv('MOYSKLAD_TOKEN', '').strip()
    if not token:
        parser.error('MOYSKLAD_TOKEN missing')
    folder = args.output / ('print-preflight-' + uuid4().hex)
    folder.mkdir(mode=0o700)
    rows = []
    with requests.Session() as session:
        session.headers.update({'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'gzip'})
        for number in args.numbers:
            try:
                row = read_order(session, number, folder)
            except PrintBlocked as exc:
                row = dict(number=number, error=str(exc))
            except (requests.RequestException, ValueError, KeyError, TypeError):
                row = dict(number=number, error='transport_or_response_error')
            rows.append(row)
    save(folder, 'summary.json', dict(at=datetime.now(timezone.utc).isoformat(), rows=rows))
    print('Evidence:', folder)
    for row in rows:
        print(row['number'], row.get('error') or
              (row.get('sample_export_blocker') or 'fresh_printed_sample_candidate'))
    return int(any(r.get('error') or r.get('sample_export_blocker') for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())

# поток: print
"""Explicit, fixed historical samples only. Never operational order selection.

Every export uses the live printed=true guard. No unprinted override exists.
Run only for the already authorized print preparation samples.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from shipment_guard.config import identity
from print_service.clients import PrintClients, reference_id
from print_service.domain import PrintBlocked, guard_sample_export
from print_service.pdf import pdf_reader
from print_service.preparer import Preparer
from print_service.storage import save_json

SAMPLES = (('oz_acc1','52441062-0172-3'), ('wb_acc1','6154980701'),
           ('wb_acc2','6157451387'), ('ya_acc1','63751218432'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    clients = PrintClients(audit_root=args.output / 'raw')
    report = []
    for account, number in SAMPLES:
        try:
            data = clients.ms_get('/entity/customerorder', params={'filter':'name=' + number,'limit':20})
            rows = data.get('rows', [])
            if data.get('meta',{}).get('size') != 1 or len(rows)!=1 or rows[0].get('name')!=number:
                raise PrintBlocked('sample_order_ambiguous')
            order = rows[0]
            if identity(order)[1] != account or len(order.get('demands',[]))!=1:
                raise PrintBlocked('sample_account_or_link_mismatch')
            did = reference_id(order['demands'][0], 'demand')
            demand = clients.ms_get('/entity/demand/' + did)
            guard_sample_export(order, demand, linked_order_id=order['id'])
            folder = args.output / account
            folder.mkdir(mode=0o700, exist_ok=True)
            save_json(folder / 'source.json', {'order':order,'demand':demand})
            raw = clients.export_receipt(order['id'],did,folder,expected_account=account,expected_number=number)
            after = json.loads((folder / 'ms_after.json').read_text())
            unchanged = after['order'].get('printed') == order.get('printed') and after['demand'].get('printed') is True
            if not unchanged: raise PrintBlocked('sample_related_print_flags_changed')
            save_json(folder / 'positions.json', Preparer(None, clients, root=args.output)._positions(did))
            report.append(dict(account=account,number=number,ok=True,pages=len(pdf_reader(raw).pages),printed_flags_unchanged=True))
            print(account,'verified: PDF received; document print flags unchanged',flush=True)
        except PrintBlocked as exc:
            report.append(dict(account=account,number=number,ok=False,error=str(exc)))
            print(account,'blocked:',str(exc),flush=True)
    save_json(args.output / 'report.json', {'at':datetime.now(timezone.utc).isoformat(),'samples':report})
    return int(any(not r['ok'] for r in report))


if __name__ == '__main__': raise SystemExit(main())

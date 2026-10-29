"""Prepare a complete, read-only Ozon auto-add removal manifest (stream: mkt).

No Ozon write endpoint is used. The file is a point-in-time target list, not an
authorization to change products. Reread targets and prices before any write.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ops.ozon_autoadd_flag_audit import scan_account


DEFAULT_DIR = Path('/opt/mp-analytics/logs/ozon_autoadd_preflight')
ACCOUNTS = ('oz_acc1', 'oz_acc2')
PRICE_FIELDS = ('price', 'old_price', 'min_price', 'currency_code',
                'auto_action_enabled', 'auto_add_to_ozon_actions_list_enabled')


def build_manifest(accounts=ACCOUNTS):
    targets = []
    rows = []
    for account in accounts:
        def collect(item):
            price = item.get('price') or {}
            targets.append({
                'account': account,
                'product_id': item['product_id'],
                'offer_id': item.get('offer_id'),
                'price_snapshot': {field: price.get(field) for field in PRICE_FIELDS},
            })
        row = scan_account(account, on_enabled=collect)
        if row['unknown']:
            raise RuntimeError(f"{account}: {row['unknown']} unknown flags; manifest aborted")
        if row['enabled'] != sum(target['account'] == account for target in targets):
            raise RuntimeError(f'{account}: target count mismatch')
        rows.append({key: row[key] for key in ('account', 'total', 'enabled', 'disabled', 'unknown', 'pages')})
    targets.sort(key=lambda target: (target['account'], target['product_id']))
    return {'at': datetime.now(timezone.utc).isoformat(), 'rows': rows, 'targets': targets}


def save_manifest(manifest, directory=DEFAULT_DIR):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    payload = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.NamedTemporaryFile(dir=directory, prefix='.pending-', delete=False) as file:
        temporary = Path(file.name)
        os.chmod(temporary, 0o600)
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    destination = directory / f"{manifest['at'].replace(':', '').replace('+', 'Z')}-{digest[:12]}.json"
    temporary.replace(destination)
    return destination, digest


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only Ozon auto-add target manifest')
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_DIR)
    args = parser.parse_args(argv)
    manifest = build_manifest()
    path, digest = save_manifest(manifest, args.output_dir)
    print(f'manifest={path} sha256={digest}')
    for row in manifest['rows']:
        print(f"{row['account']}: enabled={row['enabled']} disabled={row['disabled']} "
              f"unknown={row['unknown']} total={row['total']} pages={row['pages']}")
    print('Ozon writes=0; scheduled writes=0')


if __name__ == '__main__':
    main()

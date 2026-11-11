# поток: infra
"""Warm the shared verified supplier-price cache using the existing import reader.

Reads MS only; changes neither receipts, stock, orders nor financial tables.
"""
import json
from ops.fbs_ms_import_wb_pilot import current_purchase_prices


def main():
    prices, digest = current_purchase_prices(None)
    print(json.dumps({'supplier_price_cache': 'ready', 'prices': len(prices),
                      'receipt_digest': digest}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

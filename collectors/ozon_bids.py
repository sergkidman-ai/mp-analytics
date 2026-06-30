"""collectors/ozon_bids.py — ставки Ozon по SKU в активных кампаниях (ежедневный снимок).

GET /api/client/campaign/{id}/v2/products → products[{sku, bid(микрорубли), title, targetCir}].
Снимок на дату → ozon_bids (bid в рублях). Для вкладки «Ставки» (тренд по дням + правка).
Только аккаунты с Performance-кредами (Цифровой).

Запуск:  ./venv/bin/python collectors/ozon_bids.py [oz_acc1]
"""
import sys
import time
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                          # noqa: E402
from collectors.ozon_ads import has_creds, _token, PERF      # noqa: E402


def fetch(account):
    H = {"Authorization": f"Bearer {_token(account)}"}
    camps = requests.get(f"{PERF}/api/client/campaign", headers=H, timeout=60).json().get("list", [])
    running = [c for c in camps if c.get("state") == "CAMPAIGN_STATE_RUNNING"]
    cap = datetime.date.today().isoformat()
    recs = []
    for c in running:
        r = requests.get(f"{PERF}/api/client/campaign/{c['id']}/v2/products", headers=H, timeout=60)
        if r.status_code != 200:
            continue
        for p in r.json().get("products", []) or []:
            if not p.get("sku"):
                continue
            recs.append({"account": account, "campaign_id": str(c["id"]),
                         "campaign_title": c.get("title"), "adv_type": c.get("advObjectType"),
                         "sku": str(p["sku"]), "title": p.get("title"),
                         "bid": round(int(p.get("bid") or 0) / 1_000_000, 2),
                         "target_cir": p.get("targetCir") or 0, "captured_at": cap})
        time.sleep(0.3)
    return recs


def main(account="oz_acc1"):
    if not has_creds(account):
        print(f"Ozon ставки {account}: нет Performance-кредов — пропуск", flush=True)
        return
    print(f"Ozon ставки {account}", flush=True)
    recs = fetch(account)
    n = db.upsert("ozon_bids", recs, conflict_cols=["account", "campaign_id", "sku", "captured_at"],
                  update_cols=["campaign_title", "adv_type", "title", "bid", "target_cir"])
    print(f"Записано ставок: {n} | кампаний: {len({r['campaign_id'] for r in recs})}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1")

# разовый: 8 пар-гомоглифов — оставить карточку С ОСТАТКОМ, лишние удалить (фолбэк — архив с обнулением)
import sys, json
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import ms_api

APPLY = "--apply" in sys.argv
SCR = "/tmp/claude-0/-opt-mp-analytics/fc82fc79-e3ce-4bf0-b6d2-53279b6d8835/scratchpad"
BK = "/opt/mp-analytics/backups/prc_homoglyph_2026-08-20/before.json"
CYR = set("АВСЕНКМОРТХУасеорху")
pairs = json.load(open(f"{SCR}/homo8.json"))

def stock_all(pid):
    rows = ms_api.get("/report/stock/bystore", {"filter": f"product={ms_api.BASE}/entity/product/{pid}", "limit": 50}).get("rows", [])
    return {x["name"]: float(x.get("stock") or 0) for r in rows for x in r.get("stockByStore", []) if float(x.get("stock") or 0)}

targets, keep = [], []
for art, cards in pairs:
    full = []
    for c in cards:
        p = ms_api.get(f"/entity/product/{c['ms_id']}")
        p["_stock"] = stock_all(c["ms_id"])
        full.append(p)
    with_stock = [p for p in full if p["_stock"]]
    assert len(with_stock) <= 1, f"{art}: остаток на {len(with_stock)} карточках — руками"
    if with_stock:
        keep.append((art, with_stock[0]))
        targets += [p for p in full if p is not with_stock[0]]
    else:
        targets += full                       # остатка нет ни на одной — обе под удаление
    for p in full:
        mark = "КИР" if set(p.get("article") or "") & CYR else "лат"
        role = "ОСТАВЛЯЕМ" if with_stock and p is with_stock[0] else "убираем  "
        print(f"{art:18} {mark} {role} код {p.get('code') or '—':9} {p['id'][:8]} ост "
              f"{', '.join(f'{k} {v:g}' for k, v in p['_stock'].items()) or '—'}")

json.dump([{k: p.get(k) for k in ("id","code","externalCode","article","name","archived","barcodes","weight","buyPrice","salePrices","attributes","productFolder","uom","country","description")}
           for p in targets], open(BK, "w"), ensure_ascii=False, indent=1)
print(f"\nоставляем {len(keep)}, убираем {len(targets)}; бэкап {BK}")
if not APPLY:
    print("[проба] записи не было")
    sys.exit()

res = {"deleted": [], "archived": [], "failed": []}
for p in targets:
    try:
        ms_api.delete(f"/entity/product/{p['id']}")
        res["deleted"].append(p["id"]); print(f"  удалена {p['id'][:8]} {p.get('code')}")
    except Exception as exc:
        try:
            ms_api.put(f"/entity/product/{p['id']}",
                       {"archived": True, "article": "", "code": "", "externalCode": ""})
            res["archived"].append(p["id"])
            print(f"  архив   {p['id'][:8]} {p.get('code')} ← удаление отклонено: {str(exc)[:90]}")
        except Exception as e2:
            res["failed"].append(p["id"]); print(f"  СБОЙ    {p['id'][:8]}: {str(e2)[:120]}")
json.dump(res, open("/opt/mp-analytics/backups/prc_homoglyph_2026-08-20/result.json", "w"), ensure_ascii=False, indent=1)
print(f"\nудалено {len(res['deleted'])}, в архив {len(res['archived'])}, сбоев {len(res['failed'])}")

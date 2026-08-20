# разовый: архив 9 карточек-клонов, созданных прогоном «кириллица в артикулах» 19.08.2026
import sys, json
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core.db import query
from core import ms_api

PREF = ("7e0a6105","7e4e5260","7e8fa819","7ec2f4b3","7efae98b","7f4aa5b0","7f939bab","7fd38c29","800c7982")
APPLY = "--apply" in sys.argv
BK = "/opt/mp-analytics/backups/prc_cyr_dups_2026-08-20/before.json"

rows = query("SELECT ms_id, code, external_code, article, name FROM ms_product WHERE NOT archived AND article <> ''")
ids = {p: r["ms_id"] for p in PREF for r in rows if r["ms_id"].startswith(p)}
assert len(ids) == 9, ids

backup, bodies = [], []
for p in PREF:
    pid = ids[p]
    c = ms_api.get(f"/entity/product/{pid}")
    assert not c.get("archived"), f"{p}: уже в архиве"
    st = ms_api.get("/report/stock/bystore", {"filter": f"product={c['meta']['href']}", "limit": 50}).get("rows", [])
    left = sum(float(s.get("stock") or 0) for row in st for s in row.get("stockByStore", []))
    assert left == 0, f"{p}: остаток {left} — не архивируем"
    # страховка: живой оригинал с тем же артикулом обязан остаться
    art = (c.get("article") or "").strip()
    twins = [t for t in ms_api.get("/entity/product", {"filter": f"article={art}", "limit": 20}).get("rows", [])
             if t["id"] != pid and not t.get("archived")]
    assert twins, f"{p}: живого оригинала с артикулом «{art}» нет — не трогаем"
    backup.append({"id": pid, "code": c.get("code"), "article": art, "externalCode": c.get("externalCode"),
                   "archived": c.get("archived"), "name": c.get("name"),
                   "barcodes": c.get("barcodes"), "twins": [t["id"] for t in twins]})
    bodies.append({"meta": c["meta"], "archived": True, "article": "", "code": "", "externalCode": ""})
    print(f"  {p} | код {c.get('code'):8} | вн.{c.get('externalCode'):5} | арт {art:18} | ост 0 | оригиналов {len(twins)}")

json.dump(backup, open(BK, "w"), ensure_ascii=False, indent=1)
if not APPLY:
    print(f"\n[проба] к архивации {len(bodies)}; бэкап {BK}")
    sys.exit()
done = ms_api.post("/entity/product", bodies)
print(f"\nархивировано {len(done)} (бэкап {BK})")

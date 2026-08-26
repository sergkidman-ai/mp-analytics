# поток: prc — вывод карточки из обращения по правилу 49.
"""Карточка не дубль, а ошибка каталога: товар не наш профиль, замены ей нет.

Отличие от `tpl_arch.py`: тот выводит СТАРУЮ карточку-дубль, у которой есть новая замена
в шаблоне, и требует нуль на КАЖДОМ складе. Здесь замены нет, а гейт — нуль на НАШИХ
складах: «Удаленный склад» (зеркало остатка поставщика) и «Транзит» (товар в пути) не наши,
так же считают `recard.py` и `chip_conflict_arch*.py`.

Правило 49: архив + обнуление Артикул/Код/Внешний код + снятие штрихкодов. Прежние значения
целиком уходят в бэкап-JSON, откат построчно. Артикул поставщика после этого ОБЯЗАН уехать
в ЧС (`prices.blacklist`), иначе следующий прайс заведёт карточку заново.
"""
import sys, json, argparse
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import ms_api

NOT_OURS = ("Удаленный склад", "Транзит")

ap = argparse.ArgumentParser()
ap.add_argument("codes", nargs="+", help="коды карточек МС")
ap.add_argument("--backup", required=True)
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

backup, bodies = [], []
for code in a.codes:
    rows = [p for p in ms_api.get("/entity/product", {"filter": f"code={code}", "limit": 20}).get("rows", [])
            if not p.get("archived")]
    assert len(rows) == 1, f"{code}: живых карточек {len(rows)}, ожидалась одна"
    p = rows[0]
    st = ms_api.get("/report/stock/bystore", {"filter": f"product={p['meta']['href']}", "limit": 50}).get("rows", [])
    ours, virt = 0.0, 0.0
    for row in st:
        for s in row.get("stockByStore", []):
            q = float(s.get("stock") or 0)
            if s.get("name") in NOT_OURS:
                virt += q
            else:
                ours += q
    assert ours == 0, f"{code}: остаток на НАШИХ складах {ours} — не выводим"
    bc = [list(b.values())[0] for b in (p.get("barcodes") or [])]
    backup.append({"id": p["id"], "code": p.get("code"), "article": p.get("article"),
                   "externalCode": p.get("externalCode"), "archived": p.get("archived"),
                   "name": p.get("name"), "barcodes": p.get("barcodes")})
    bodies.append({"meta": p["meta"], "archived": True,
                   "article": "", "code": "", "externalCode": "", "barcodes": []})
    print(f"  {code} → архив | арт «{p.get('article')}» | вн.{p.get('externalCode')} | "
          f"штрихкоды {bc} | наши склады {ours}, зеркало поставщика {virt}")
    print(f"      {(p.get('name') or '')[:70]}")

json.dump(backup, open(a.backup, "w"), ensure_ascii=False, indent=1)
if not a.apply:
    print(f"\n[проба] к выводу {len(bodies)}; бэкап {a.backup}")
    sys.exit()
done = ms_api.post("/entity/product", bodies)
print(f"\nвыведено из обращения {len(done)} (бэкап {a.backup})")
print("НЕ ЗАБЫТЬ: артикул поставщика — в ЧС, иначе прайс заведёт карточку заново")

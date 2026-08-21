# поток: prc — вывод из обращения карточек Аквамарина со старым написанием артикула
# (`AQ …`, `OD-…`, `ODR-…`) по правилу 49: ключевые характеристики не правим на месте,
# карточку убираем в архив с обнулением Артикул/Код/Внешний код и снятием штрихкодов.
# Гейт перед записью: остаток на НАШИХ складах (не «Удаленный»/«Транзит») обязан быть 0 —
# иначе нужна пара Списание+Оприходование (правило 49 п.3), а это отдельная операция.
import json, pathlib, sys, time
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import db, ms_api

HERE = pathlib.Path(__file__).parent
B = "https://api.moysklad.ru/api/remap/1.2/entity/product/"
NOT_OURS = ("Удаленный склад", "Транзит")
apply = "--apply" in sys.argv

ids = [c["id"] for c in json.loads((HERE.parent / "prc_aq_suffix_2026-08-21" / "before.json").read_text(encoding="utf-8"))]
cards = db.query("""SELECT ms_id, code, article, external_code ext, name FROM ms_product
                     WHERE NOT archived AND (code ~ '^[0-9]+od$' OR ms_id = ANY(%s)) ORDER BY code""", (ids,))
before, done, held, fail = [], [], [], []
for c in cards:
    ours = {}
    for r in ms_api.get("/report/stock/bystore", {"filter": f'product={B}{c["ms_id"]}'}).get("rows", []):
        for s in r.get("stockByStore", []):
            if s.get("stock") and s["name"] not in NOT_OURS:
                ours[s["name"]] = s["stock"]
    if ours:
        held.append((c["code"], c["article"], ours)); continue
    card = ms_api.get(f'/entity/product/{c["ms_id"]}')
    before.append(card)
    if not apply:
        done.append((c["code"], c["article"])); continue
    try:
        ms_api.put(f'/entity/product/{c["ms_id"]}',
                   {"archived": True, "article": "", "code": "", "externalCode": "", "barcodes": []})
        done.append((c["code"], c["article"]))
    except Exception as exc:
        fail.append((c["code"], str(exc)[:150]))
    time.sleep(0.2)
(HERE / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=1), encoding="utf-8")
(HERE / "result.json").write_text(json.dumps({"apply": apply, "done": done, "held": held, "fail": fail},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
print(f'{"ЗАПИСЬ" if apply else "ПРОБА"}: карточек {len(cards)}, в архив {len(done)}, '
      f'держим (есть наш остаток) {len(held)}, сбоев {len(fail)}')
for c, a, st in held[:5]: print("  ДЕРЖИМ", c, a, st)
for c, why in fail[:5]: print("  СБОЙ", c, why)

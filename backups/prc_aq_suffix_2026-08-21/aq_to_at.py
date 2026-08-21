# поток: prc — суффикс кода Аквамарина: `aq` → `at` (решение Сергея 21.08.2026).
# Меняем ТОЛЬКО код карточки. Артикул не трогаем (правило 45 — пишем как в прайсе:
# `AQ 2085` и `AQ TK-17/18/100` в прайсе Одиссея живы). Внешний код, штрихкод, поля — тоже.
# Совпадение кода с уже существующей карточкой `…at` того же товара законно (правило 44:
# код повторяться может, разводит артикул).
import json, pathlib, sys, time
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import db, ms_api

HERE = pathlib.Path(__file__).parent
apply = "--apply" in sys.argv
rows = db.query("""SELECT ms_id, code, external_code ext, article, name FROM ms_product
                    WHERE code ~ '^[0-9]+aq$' AND NOT archived ORDER BY code""")
before, done, fail = [], [], []
for r in rows:
    card = ms_api.get(f'/entity/product/{r["ms_id"]}')
    before.append(card)
    new = card["code"][:-2] + "at"
    if not apply:
        done.append((card["code"], new, card.get("article"))); continue
    try:
        ms_api.put(f'/entity/product/{r["ms_id"]}', {"code": new})
        done.append((card["code"], new, card.get("article")))
    except Exception as exc:
        fail.append((card["code"], str(exc)[:120]))
    time.sleep(0.2)
(HERE / "before.json").write_text(json.dumps(before, ensure_ascii=False, indent=1), encoding="utf-8")
(HERE / "result.json").write_text(json.dumps({"apply": apply, "done": done, "fail": fail},
                                             ensure_ascii=False, indent=1), encoding="utf-8")
print(f'{"ЗАПИСЬ" if apply else "ПРОБА"}: карточек {len(rows)}, переименовано {len(done)}, сбоев {len(fail)}')
for c, why in fail[:5]:
    print("  СБОЙ", c, why)

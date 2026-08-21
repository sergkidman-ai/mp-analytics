# поток: prc — заведение новой карточки взамен архивированной (правило 49 п.2):
# не копия старой, а сборка с нуля машинерией прайс-конвейера (поля от родни по внешнему
# коду, вес из прайсов, «Название WB» по правилам). Артикул пишем ровно как в прайсе (45).
import sys, json, pathlib, datetime as dt
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import db
from prices import ms_import
from prices.profiles import get_profile
from prices.cbr import effective_rate

HERE = pathlib.Path(__file__).parent
ART, CODE = "AQ TK-17/18/100", "0120at"
NAME = ("Картридж для Kyocera TK17/18/100  FS1010/1050/1020D/1018MFP/KM1500 "
        "7.2К Aquamarine (Совместимый)")
apply = "--apply" in sys.argv

prof = get_profile("odissey")
rate, rate_date = effective_rate(dt.date.today(), prof.currency, prof.markup)
src = db.query("SELECT price_src FROM prc_price_row WHERE load_id=69 AND article=%s", (ART,))[0]["price_src"]
rub = round(float(src) * float(rate), 2)

row = db.query("SELECT id FROM prc_novelty WHERE supplier_key='odissey' AND article=%s", (ART,))
if not row:
    row = db.query("""INSERT INTO prc_novelty (supplier_key, article_norm, article, name, price_rub,
                                               first_seen, last_seen, decision, ms_code, decided_at)
                      VALUES ('odissey', %s, %s, %s, %s, now(), now(), 'matched', %s, now())
                      RETURNING id""", (ART.upper(), ART, NAME, rub, CODE))
nid = row[0]["id"]
records, notes = ms_import.build("odissey", ("matched",), ids=[nid])
print(f"курс {float(rate):.4f} ₽/{prof.currency} (ЦБ на {rate_date}) → цена {rub} ₽; строк {len(records)}")
for _, art, flags in notes:
    for f in flags: print("  замечание:", art, "—", f)
if records:
    r = records[0]
    print({k: r.get(k) for k in ("Код", "Внешний код", "Артикул", "Вес", "Штрихкод Code128",
                                 "Закупочная цена", "Наименование")})
created = ms_import.create_in_ms(records, ms_import.supplier_of(ART, prof, NAME), dry=not apply)
(HERE / "new_card.json").write_text(json.dumps({"novelty": nid, "records": records,
                                                "created": created, "apply": apply},
                                               ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print("создано:", created if apply else "(проба)")

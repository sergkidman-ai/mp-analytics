"""collectors/rapid_dims.py — габариты поставщика RAPID (b2b-rapid1.ru) по API → supplier_dims.

В отличие от collectors/supplier_dims.py (ручные xlsx-выгрузки), этот поставщик отдаёт прайс
по HTTP JSON. Кладём разобранные габариты в ту же таблицу supplier_dims под supplier='rapid'.

Источник: GET https://b2b-rapid1.ru/api/export.php?authkey=<RAPID_API_KEY>&type=json
Ключ — в .env (RAPID_API_KEY). Сырьё дампим в incoming/gab/ для провенанса (rule 2: raw отдельно).

Поля позиции (price[]):
  CodeID          — OEM-модель картриджа (напр. DR-1075) → article (ключ связки, 98% заполнено);
  Ean             — штрихкод → barcode (39%);
  GabarityDlina/Shirina/Visota — Д/Ш/В в МЕТРАХ (0.17 = 17 см); полные ДхШхВ у ~24%;
  Volume          — объём в м³ (заполнен и там, где ДхШхВ=0) → литры, расширяет покрытие;
  Weight          — вес в кг; Name — заголовок; Vendor/SubID* — бренд/категории.

Единицы приводим к см/кг/л теми же санитайзерами, что в supplier_dims (0<см<=150, 0.02<=л<=60).
Дедуп по (supplier=rapid, article=CodeID): предпочитаем запись С габаритами/объёмом.
Идемпотентно: DELETE supplier='rapid' + upsert (как в supplier_dims.build).

Запуск:  ./venv/bin/python collectors/rapid_dims.py
"""
import os
import sys
import json
import pathlib
import datetime

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")
from core import db  # noqa: E402
# переиспользуем санитайзеры единиц из ручного коллектора
from collectors.supplier_dims import _num, _cm, _sane_vol, _vol_l  # noqa: E402

EXPORT_URL = "https://b2b-rapid1.ru/api/export.php"
RAW_DIR = BASE_DIR / "incoming" / "gab"


def fetch(save_raw=True):
    """Тянем JSON-прайс; при save_raw дампим сырьё в incoming/gab/. Возвращает price[]."""
    key = os.getenv("RAPID_API_KEY")
    if not key:
        raise SystemExit("RAPID_API_KEY не задан в .env")
    r = requests.get(EXPORT_URL, params={"authkey": key, "type": "json"}, timeout=180)
    r.raise_for_status()
    data = r.json()
    err = data.get("error") or {}
    if err.get("code") not in (0, None):
        raise SystemExit(f"RAPID API error: {err}")
    if save_raw:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.date.today().isoformat()
        (RAW_DIR / f"rapid_{stamp}.json").write_text(
            json.dumps(data, ensure_ascii=False))
    return data.get("price", [])


def _m_to_cm(v):
    """Габарит из метров → см. Валидируем через _cm (0<см<=150), мусор → None."""
    x = _num(v)
    return _cm(x * 100.0) if x else None


def parse(price):
    """price[] → строки supplier_dims (дедуп по article, приоритет записи с габаритами)."""
    rows = {}
    for p in price:
        art = (p.get("CodeID") or "").strip()
        if not art:
            continue
        L = _m_to_cm(p.get("GabarityDlina"))
        W = _m_to_cm(p.get("GabarityShirina"))
        H = _m_to_cm(p.get("GabarityVisota"))
        vol = _vol_l(L, W, H)                       # из ДхШхВ, если полные
        if vol is None:                             # иначе из поля Volume (м³)
            vm3 = _num(p.get("Volume"))
            vol = _sane_vol(vm3 * 1000.0) if vm3 else None
        wt = _num(p.get("Weight"))
        ean = (p.get("Ean") or "").strip() or None
        rec = {
            "supplier": "rapid", "article": art, "barcode": ean,
            "length_cm": L, "width_cm": W, "height_cm": H,
            "weight_kg": round(wt, 3) if wt else None,
            "volume_l": vol,
            "title": (p.get("Name") or "").strip() or None,
        }
        # дедуп: держим лучшую запись — с полными ДхШхВ > с объёмом > пустую
        prev = rows.get(art)
        if prev is None or _score(rec) > _score(prev):
            rows[art] = rec
    return list(rows.values())


def _score(r):
    return (2 if (r["length_cm"] and r["width_cm"] and r["height_cm"]) else 0) + \
           (1 if r["volume_l"] else 0)


def build():
    price = fetch()
    rows = parse(price)
    stamp = datetime.date.today().isoformat()
    for r in rows:
        r["src_file"] = f"rapid_{stamp}.json"
    db.execute("DELETE FROM supplier_dims WHERE supplier=%s", ("rapid",))
    db.upsert("supplier_dims", rows, conflict_cols=["supplier", "article"])
    withdim = sum(1 for r in rows if r["length_cm"] and r["width_cm"] and r["height_cm"])
    withvol = sum(1 for r in rows if r["volume_l"])
    withbc = sum(1 for r in rows if r["barcode"])
    print(f"  [rapid_dims] price {len(price)} → {len(rows)} артикулов; "
          f"полные ДхШхВ {withdim}, с объёмом {withvol}, со штрихкодом {withbc}", flush=True)
    return len(rows)


if __name__ == "__main__":
    build()

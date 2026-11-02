"""collectors/set_cost.py — себестоимость наборов = Σ закупочных компонентов.

Состав набора → thecartridge.ru POST /api/catalog/mix_data {external_code} (отдаёт список
external_code компонентов; для простого артикула — {"error":"not_mix"}). Цена компонента →
МойСклад (ms_product.buy_price, min по external_code). Себест набора = Σ цен компонентов.

Состав КЕШИРУЕТСЯ в set_cost (резолвим через API только новые наборы), цена пересчитывается
из МС каждый прогон без API. Кандидаты — наборы (по названию), заведённые на МП (ВБ/Озон).

Запуск:  ./venv/bin/python collectors/set_cost.py [--refresh-all]
"""
import os
import sys
import time
import datetime
import pathlib
import requests

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = os.getenv("CARTRIDGE_API_URL", "https://thecartridge.ru/api/catalog/mix_data")
PAUSE = 0.75              # новый ТК сообщает лимит 60 запросов/мин; держим запас
SESSION = requests.Session()       # повторно используем TLS-соединение для сотен составов


class RateLimited(RuntimeError):
    pass


class UnknownCode(RuntimeError):
    pass


def _mix(ec):
    """Состав набора: список external_code; None только для not_mix/пустого списка."""
    key = os.getenv("CARTRIDGE_API_KEY")
    if not key:
        raise RuntimeError('CARTRIDGE_API_KEY не задан')
    last_error = None
    for attempt in range(3):
        try:
            r = SESSION.post(API, headers={"Api-Key": key},
                             json={"external_code": str(ec)}, timeout=30)
            if r.status_code == 429:
                if attempt == 2:
                    raise RateLimited('mix_data: лимит запросов после повторов')
                time.sleep(65)     # API не отдаёт Retry-After; окно лимита — минута
                continue
            if r.status_code == 422:
                detail = r.json()
                if isinstance(detail, dict) and 'external_code' in (detail.get('errors') or {}):
                    raise UnknownCode(f'mix_data {ec}: код отсутствует в ТК')
            r.raise_for_status()
            d = r.json()
        except (RateLimited, UnknownCode):
            raise
        except Exception as exc:
            last_error = type(exc).__name__
            time.sleep(2 ** attempt)
            continue
        if isinstance(d, list) and d:
            codes = [(x.get('external_code') if isinstance(x, dict) else x) for x in d]
            codes = [str(x).strip() if x is not None else '' for x in codes]
            if any(not x for x in codes):
                raise RuntimeError(f'mix_data {ec}: компонент без external_code')
            return codes
        if isinstance(d, dict) and d.get('error') == 'not_mix':
            return None
        if d == []:
            return None
        raise RuntimeError(f'mix_data {ec}: неожиданный ответ')
    raise RuntimeError(f'mix_data {ec}: {last_error}')


def main(refresh_all=False, all_ms_named=False):
    # 1) кандидаты: наборы (по названию), заведённые на ВБ или Озон
    if all_ms_named:
        # Восстановление без прежних MP-витрин: ТК хранит состав по базовому
        # четырёхзначному внешнему коду, а МС содержит производные карточки.
        cands = [r["ec"] for r in db.query("""
            SELECT DISTINCT left(p.external_code, 4) ec FROM ms_product p
            WHERE (p.name ILIKE '%набор%' OR p.name ILIKE '%комплект%')
              AND p.external_code ~ '^[0-9]{4}'""")]
    else:
        cands = [r["ec"] for r in db.query("""
            SELECT DISTINCT p.external_code ec FROM ms_product p
            WHERE (p.name ILIKE '%набор%' OR p.name ILIKE '%комплект%') AND p.external_code IS NOT NULL
              AND (EXISTS(SELECT 1 FROM wb_cards w WHERE w.vendor_code=p.external_code)
                OR EXISTS(SELECT 1 FROM ozon_product o WHERE o.offer_id=p.external_code))""")]
    # 2) цена компонентов из МС (min buy_price по external_code)
    price = {r["external_code"]: float(r["bp"]) for r in db.query(
        "SELECT external_code, min(buy_price) bp FROM ms_product WHERE buy_price>0 AND external_code IS NOT NULL GROUP BY external_code")}
    # 3) кешированный состав
    cached = {r["external_code"]: r["components"] for r in db.query(
        "SELECT external_code, components FROM set_cost WHERE components IS NOT NULL")}

    recs, resolved, api_calls, errors, unknown, saved, last_report = [], 0, 0, [], [], 0, 0
    for ec in cands:
        comps = None if refresh_all else cached.get(ec)
        if comps is None:
            try:
                comps = _mix(ec)
            except RateLimited:
                errors.append(ec)
                print(f'  [set_cost] лимит ТК на коде {ec}; сохраняю прочитанное', flush=True)
                break
            except UnknownCode:
                unknown.append(ec)
                comps = None
            except RuntimeError:
                errors.append(ec)
                comps = None
            api_calls += 1; time.sleep(PAUSE)
            if comps:
                resolved += 1
        if comps:
            covered = [c for c in comps if c in price]
            cost = round(sum(price[c] for c in covered), 2)
            recs.append({"external_code": ec, "components": comps, "n_components": len(comps),
                         "cost": cost, "covered": len(covered),
                         "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if api_calls and api_calls % 50 == 0 and api_calls != last_report:
            if len(recs) > saved:
                db.upsert("set_cost", recs[saved:], conflict_cols=["external_code"],
                          update_cols=["components", "n_components", "cost", "covered", "resolved_at"])
                saved = len(recs)
            print(f"  [set_cost] API-запросов {api_calls}, наборов собрано {len(recs)}, "
                  f"неизвестных ТК {len(unknown)}, ошибок {len(errors)}", flush=True)
            last_report = api_calls
        if len(errors) >= 10:
            print('  [set_cost] десять ошибок чтения; сохраняю прочитанное и останавливаюсь', flush=True)
            break
    if len(recs) > saved:
        db.upsert("set_cost", recs[saved:], conflict_cols=["external_code"],
                  update_cols=["components", "n_components", "cost", "covered", "resolved_at"])
    full = sum(1 for r in recs if r["covered"] == r["n_components"])
    print(f"Себест наборов: {len(recs)} наборов | API-запросов {api_calls} (новых составов {resolved}) | "
          f"полное покрытие компонентов: {full}/{len(recs)}", flush=True)
    print(f"Коды, которых нет в ТК: {len(unknown)} {unknown}", flush=True)
    if errors:
        raise RuntimeError(f'mix_data не прочитан для {len(errors)} кодов: {errors[:20]}')


if __name__ == "__main__":
    main(refresh_all="--refresh-all" in sys.argv,
         all_ms_named="--all-ms-named" in sys.argv)

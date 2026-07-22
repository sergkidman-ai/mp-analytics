# поток: mkt
"""collectors/thecartridge_prices.py — живая «восстановительная» себестоимость с нашей
платформы TheCartridge (thecartridge.ru /api/catalog/best).

POST /api/catalog/best  {"external_codes":[...]}  header Api-Key  →  {код:{buy_price}}.
  - buy_price число → status='ok';  buy_price=null → status='no_lu' (ЛУ нет в моменте, НЕ ноль).
  - Жёсткий лимит API — 100 кодов/запрос (101 → HTTP 422 «Превышено допустимое количество»).
  - Цены динамичные → пишем ИСТОРИЮ по дням в tc_buy_price (PK captured_date+external_code).

Универсум по умолчанию — external_code, СВЯЗАННЫЕ хотя бы с одной МП-карточкой (WB/Ozon/ЯМ):
экономим API и покрываем всё, что нужно для маржи-контроля. `--all` — все non-archived коды.

Это ВТОРАЯ себестоимость рядом с FIFO из отгрузок МС (fin, read-only). buy_price = «почём купим
сегодня» (решения mkt); FIFO = факт для отчётности. Не пишем в margin_by_sku/ms_product.

Сырьё каждого прогона (полный ответ {код:buy_price}) сохраняем в reports/data/tc_best_raw_<дата>.json;
в чат — только сводка (правило 11).

Запуск:  ./venv/bin/python collectors/thecartridge_prices.py [--all] [--date YYYY-MM-DD]
"""
import os
import sys
import json
import time
import datetime
import pathlib
import urllib.request
import urllib.error

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")

URL = "https://thecartridge.ru/api/catalog/best"
BATCH = 100        # жёсткий потолок API (101 → 422)
PAUSE = 0.4        # пауза между батчами
RETRIES = 4        # ретраи на 429/5xx/timeout (422 НЕ ретраим — это ошибка размера/тела)
RL_BACKOFF = 8     # базовая пауза (сек) на HTTP 429 — платформа лимитирует при длинной серии
RAW_DIR = BASE_DIR / "reports" / "data"


def _key():
    k = os.getenv("CARTRIDGE_API_KEY")
    if not k:
        raise RuntimeError("CARTRIDGE_API_KEY не задан в .env")
    return k


def _post(codes, key):
    """Один батч → dict {код: buy_price|None}. Ретраи на сеть/5xx/429, 422 пробрасывает сразу."""
    body = json.dumps({"external_codes": codes}).encode()
    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Api-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.loads(r.read())
            # {код:{buy_price:N|null}} → {код: N|None}
            return {str(k): (v.get("buy_price") if isinstance(v, dict) else v) for k, v in d.items()}
        except urllib.error.HTTPError as e:
            if e.code == 422:      # наш промах по размеру/телу — ретрай не поможет
                raise
            last = f"HTTP {e.code}"
            if e.code == 429:      # рейт-лимит: ждём заметно дольше (нарастающе)
                time.sleep(RL_BACKOFF * (attempt + 1))
                continue
        except Exception as e:     # noqa: BLE001 — сеть/timeout/JSON
            last = f"{type(e).__name__}"
        time.sleep(1 + attempt)    # линейный backoff на сеть/5xx
    raise RuntimeError(f"батч не удался после {RETRIES} попыток: {last}")


def _universe(all_codes=False):
    """external_code для сбора. По умолчанию — связанные с МП-карточкой (WB/Ozon/ЯМ)."""
    if all_codes:
        rows = db.query("""
            SELECT DISTINCT external_code ec FROM ms_product
            WHERE external_code IS NOT NULL AND NOT archived""")
        return [r["ec"] for r in rows]
    rows = db.query("""
        SELECT DISTINCT p.external_code ec
        FROM ms_product p
        WHERE p.external_code IS NOT NULL AND NOT p.archived AND (
              EXISTS (SELECT 1 FROM wb_cards w     WHERE w.vendor_code = p.external_code)
           OR EXISTS (SELECT 1 FROM ozon_product o WHERE o.offer_id    = p.external_code)
           OR EXISTS (SELECT 1 FROM raw_yandex_offer y WHERE y.offer_id = p.external_code)
        )""")
    codes = {r["ec"] for r in rows}
    # + префиксы vendorCode ВБ: 5+-значный числовой артикул = <4 цифры код товара платформы><цифра
    #   цвета> (FBO-префиксное правило). Первые 4 знака — код товара; добавляем как валидный код.
    for r in db.query("""
        SELECT DISTINCT substr(vendor_code,1,4) ec FROM wb_cards
        WHERE length(vendor_code)>=5 AND vendor_code ~ '^[0-9]+$'"""):
        codes.add(r["ec"])
    return sorted(codes)


def main(all_codes=False, on_date=None):
    key = _key()
    day = on_date or datetime.date.today().isoformat()
    codes = _universe(all_codes)
    print(f"[thecartridge] универсум {len(codes)} кодов "
          f"({'все non-archived' if all_codes else 'связанные с МП'}), дата {day}", flush=True)

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    raw, missed = {}, []
    t0 = time.time()

    def _run(code_list, label=""):
        got = {}
        for i in range(0, len(code_list), BATCH):
            chunk = code_list[i:i + BATCH]
            try:
                res = _post(chunk, key)
            except Exception as e:  # noqa: BLE001 — не роняем прогон из-за одного батча
                missed.extend(chunk)
                print(f"  [{label}batch {i//BATCH}] пропущен: {e}", flush=True)
                continue
            for ec in chunk:
                got[ec] = res.get(ec)
            if (i // BATCH) % 20 == 0 and i:
                print(f"  {label}собрано {i+len(chunk)}/{len(code_list)}", flush=True)
            time.sleep(PAUSE)
        return got

    raw.update(_run(codes))
    # пере-заход по кодам из сбойных батчей (обычно транзиентный 429 к концу серии)
    if missed:
        retry_codes = list(missed)
        missed.clear()
        print(f"  пере-заход по {len(retry_codes)} пропущенным кодам после паузы…", flush=True)
        time.sleep(RL_BACKOFF)
        raw.update(_run(retry_codes, label="retry "))

    recs, n_ok, n_null = [], 0, 0
    for ec, bp in raw.items():
        if bp is None:
            recs.append({"captured_date": day, "external_code": ec,
                         "buy_price": None, "status": "no_lu", "captured_at": now})
            n_null += 1
        else:
            recs.append({"captured_date": day, "external_code": ec,
                         "buy_price": float(bp), "status": "ok", "captured_at": now})
            n_ok += 1
    n_fail_batches = len(missed)

    if recs:
        db.upsert("tc_buy_price", recs, conflict_cols=["captured_date", "external_code"],
                  update_cols=["buy_price", "status", "captured_at"])
    # сырьё — в файл, не в чат
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"tc_best_raw_{day}.json"
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    dt = time.time() - t0
    print(f"[thecartridge {day}] записано {len(recs)}: ЛУ есть {n_ok}, "
          f"нет ЛУ {n_null}; кодов не добрано {n_fail_batches}; {dt:.0f}с; сырьё → {raw_path.name}",
          flush=True)
    return len(recs)


if __name__ == "__main__":
    args = sys.argv[1:]
    all_flag = "--all" in args
    dt = None
    if "--date" in args:
        dt = args[args.index("--date") + 1]
    main(all_codes=all_flag, on_date=dt)

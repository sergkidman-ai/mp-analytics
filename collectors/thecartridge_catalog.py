# поток: prc
"""collectors/thecartridge_catalog.py — слепок НАШЕГО каталога в TheCartridge.

POST /api/catalog/cartridge_models  {"page":N,"limit":200}  header Api-Key  →  список моделей.
Отдаются модели, привязанные к внешнему коду; связь строго 1:1 — внешний код заполнен и уникален.

Зачем: до сих пор все шесть признаков сверки «строка прайса ↔ наш товар» разбирались из НАЗВАНИЙ
карточек МойСклада, а МС — это товары поставщиков, заведённые руками (названия неполные и местами
ошибочные). Каталог ТК — первоисточник: одна карточка на внешний код, характеристики полями.

В API на каждый разбор новинок не ходим: слепок обновляется ночным таймером prc-tc-catalog
(34 запроса в сутки), а матчинг читает `prc_tc_model` / `prc_tc_code` из БД.

Признаки приводим к НАШЕЙ кодировке (prices/features.py) прямо здесь, чтобы матчер не разбирал
их заново на каждом прогоне. Цвет и бренд гоняем через те же функции, которыми разбираются
названия МС: одинаковый разбор с обеих сторон сравнения важнее, чем более богатый словарь
(экзотика вроде «Усилитель глянца» одинаково даёт None и там, и там — значит, ложного конфликта
не будет).

Запуск:  ./venv/bin/python collectors/thecartridge_catalog.py [--dry]
"""
import os
import sys
import json
import time
import argparse
import pathlib
import urllib.request
import urllib.error

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db          # noqa: E402
from prices import features as F  # noqa: E402

load_dotenv(BASE_DIR / ".env")

URL = "https://thecartridge.ru/api/catalog/cartridge_models"
LIMIT = 200        # потолок API для поля limit
MAX_PAGES = 60     # предохранитель от бесконечного цикла (на 13.08.2026 хватало 34 страниц)
PAUSE = 1.0        # пауза между страницами — у них рабочие процессы, не долбим
RETRIES = 4        # ретраи на 429/5xx/сеть
RL_BACKOFF = 8     # базовая пауза (сек) на HTTP 429

# «Чип без счётчика» у нас отдельное значение (features.chip), и схлопывать его в «с чипом»
# нельзя: это разный товар для покупателя. Чтобы такая разница не выбрасывала верную карточку,
# сравнение чипа их не противопоставляет (см. chip_ok в prices/catalog.py) — но хранится факт
# как есть. Первая буква «с/c» приходит и кириллицей, и латиницей — ловим оба написания.
CHIP_MAP = {"с чипом": "chip", "c чипом": "chip",
            "с чипом без счетчика": "chip_free", "c чипом без счетчика": "chip_free",
            "без чипа": "nochip"}

MODEL_COLS = ("external_code", "title", "additional_title", "united_title", "similar_title",
              "consumable_type", "color", "resource", "chip", "brand", "printer_models",
              "weight_g", "height_mm", "width_mm", "depth_mm", "volume_ml", "ved_code",
              "best_before_days", "firmware_limit", "raw")


def _key():
    k = os.getenv("CARTRIDGE_API_KEY")
    if not k:
        raise RuntimeError("CARTRIDGE_API_KEY не задан в .env")
    return k


def _post(page, key):
    """Одна страница каталога → список моделей. Ретраи на сеть/5xx/429."""
    body = json.dumps({"page": page, "limit": LIMIT}).encode()
    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Api-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            if isinstance(d, dict):          # на случай обёртки {"data":[...]}
                d = d.get("data") or d.get("items") or []
            return list(d)
        except urllib.error.HTTPError as e:
            if e.code == 422:                # наш промах по телу запроса — ретрай не поможет
                raise
            last = f"HTTP {e.code}"
            if e.code == 429:
                time.sleep(RL_BACKOFF * (attempt + 1))
                continue
        except Exception as e:               # noqa: BLE001 — сеть/timeout/JSON
            last = f"{type(e).__name__}"
        time.sleep(1 + attempt)
    raise RuntimeError(f"страница {page} не удалась после {RETRIES} попыток: {last}")


def _int(v):
    """Число или None. У них 0 в габаритах/ресурсе означает «не заполнено», не ноль."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n or None


def _color(inks):
    """Цвет модели в нашей кодировке. Список из >1 цвета — это НАБОР, а не цвет: None."""
    inks = inks or []
    return F.color(inks[0]) if len(inks) == 1 else None


def _brands(printer_models):
    """Бренды принтеров, сведённые к нашему канону (features.brand_canon)."""
    out = set()
    for pm in printer_models or []:
        b = F.brand_canon(pm.get("brand") if isinstance(pm, dict) else pm)
        if b:
            out.add(b)
    return sorted(out)


def parse(rec):
    """Модель из API → строка prc_tc_model (признаки уже в нашей кодировке)."""
    vol = rec.get("volume_ml")
    return {
        "external_code": str(rec.get("external_code")).strip(),
        "title": (rec.get("title") or "").strip(),
        "additional_title": (rec.get("additional_title") or "").strip() or None,
        "united_title": (rec.get("united_title") or "").strip() or None,
        "similar_title": (rec.get("similar_title") or "").strip() or None,
        "consumable_type": (rec.get("consumable_type") or "").strip() or None,
        "color": _color(rec.get("ink_colors")),
        "resource": _int(rec.get("max_pages")),
        "chip": CHIP_MAP.get(str(rec.get("chip") or "").strip().lower()),
        "brand": _brands(rec.get("printer_models")),
        "printer_models": json.dumps(rec.get("printer_models") or [], ensure_ascii=False),
        "weight_g": _int(rec.get("weight")),
        "height_mm": _int(rec.get("height")),
        "width_mm": _int(rec.get("width")),
        "depth_mm": _int(rec.get("depth")),
        "volume_ml": vol if vol not in ("", 0) else None,
        "ved_code": (rec.get("ved_eaes_code") or "").strip() or None,
        "best_before_days": _int(rec.get("best_before_days")),
        "firmware_limit": None if rec.get("firmware_limit") in (None, "") else str(rec["firmware_limit"]),
        "raw": json.dumps(rec, ensure_ascii=False),
    }


def codes_of(row):
    """Коды модели для индекса матчинга: features.codes() по всем четырём полям названия.

    Одно и то же поле у разных моделей даёт разные коды, поэтому источник кода сохраняем —
    по нему потом видно, чем именно совпало (title или синоним из «Доп. названия»).
    """
    out = {}
    for src in ("title", "additional_title", "united_title", "similar_title"):
        for c in F.codes(row.get(src) or ""):
            out.setdefault(c, src)          # первое (более надёжное) поле выигрывает
    return out


def fetch(key):
    """Все страницы каталога до первой пустой. Дубли по внешнему коду схлопываем."""
    models, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        batch = _post(page, key)
        if not batch:
            break
        for rec in batch:
            ec = str(rec.get("external_code") or "").strip()
            if not ec or ec in seen:
                continue
            seen.add(ec)
            models.append(rec)
        if page < MAX_PAGES:
            time.sleep(PAUSE)
    else:
        print(f"[tc-catalog] ВНИМАНИЕ: упёрлись в предохранитель {MAX_PAGES} страниц", flush=True)
    return models


def save(rows):
    """Upsert моделей + перезалив индекса кодов. Пропавшие не удаляем, ставим gone_at."""
    codes = []
    for r in rows:
        for code, src in codes_of(r).items():
            codes.append({"code": code, "external_code": r["external_code"], "source": src})

    have = {r["external_code"] for r in db.query("SELECT external_code FROM prc_tc_model")}
    fresh = {r["external_code"] for r in rows}
    new = len(fresh - have)

    db.upsert("prc_tc_model", [{c: r[c] for c in MODEL_COLS} for r in rows], ["external_code"])
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE prc_tc_model SET last_seen = now(), gone_at = NULL "
                        "WHERE external_code = ANY(%s)", (sorted(fresh),))
            cur.execute("UPDATE prc_tc_model SET gone_at = now() "
                        "WHERE gone_at IS NULL AND NOT (external_code = ANY(%s))", (sorted(fresh),))
            gone = cur.rowcount
            # Индекс кодов перезаливаем целиком: правка названия в ТК должна убирать старый код,
            # а не оставлять его вечно висеть на модели.
            cur.execute("DELETE FROM prc_tc_code WHERE external_code = ANY(%s)", (sorted(fresh),))
    db.upsert("prc_tc_code", codes, ["code", "external_code"])
    return new, gone, len(codes)


def main():
    ap = argparse.ArgumentParser(description="Слепок каталога TheCartridge")
    ap.add_argument("--dry", action="store_true", help="только собрать и посчитать, не писать в БД")
    args = ap.parse_args()

    t0 = time.time()
    raw = fetch(_key())
    rows = [parse(r) for r in raw if str(r.get("external_code") or "").strip()]
    pages = -(-len(raw) // LIMIT)

    filled = {k: sum(1 for r in rows if r[k] not in (None, [], ""))
              for k in ("color", "resource", "chip", "brand")}
    print(f"[tc-catalog] страниц {pages}, моделей {len(rows)}, {time.time() - t0:.0f} c")
    print(f"[tc-catalog] признаки: цвет {filled['color']}, ресурс {filled['resource']}, "
          f"чип {filled['chip']}, бренд {filled['brand']}")
    if args.dry:
        print(f"[tc-catalog] --dry: в БД не пишем, кодов для индекса "
              f"{sum(len(codes_of(r)) for r in rows)}")
        return
    new, gone, ncodes = save(rows)
    print(f"[tc-catalog] записано {len(rows)} (новых {new}), кодов {ncodes}, "
          f"помечено пропавшими {gone}")


if __name__ == "__main__":
    main()

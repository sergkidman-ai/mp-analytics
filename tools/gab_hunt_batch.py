#!/usr/bin/env python3
# поток: gab
"""tools/gab_hunt_batch.py — обвязка точечной охоты по непокрытым моделям.

Два шага, между которыми стоит независимая валидация:
  candidates  — extract.jsonl (CANDIDATE от deepseek_extract) → candidate.csv
                в формате tools/deepseek_candidate_validator.py
                (_structural_check=READY_FOR_VALIDATION, размеры приведены к см);
  summary     — по confirmed.csv валидатора: найдено/не найдено, закрытая
                непокрытая выручка (в т.ч. накопительно по всем партиям),
                расход токенов ДипСика.

Сам ничего не отправляет на маркетплейсы. CONFIRMED ставит только валидатор.

  ./venv/bin/python -m tools.gab_hunt_batch candidates --batch docs/web_search_v2/hunt/batch_001
  ./venv/bin/python -m tools.gab_hunt_batch summary    --batch docs/web_search_v2/hunt/batch_001
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

MODELS_CSV = BASE_DIR / "docs/selling_uncovered_models.csv"
HUNT_DIR = BASE_DIR / "docs/web_search_v2/hunt"

CAND_COLS = ["vendorCode", "model", "manufacturer", "oem_code", "url", "title",
             "dimensions_cm", "original_dimensions", "original_unit", "dimension_type",
             "evidence", "fetch_method", "_structural_check"]

# цены за 1M токенов, $ (api-docs.deepseek.com и platform.claude.com, сверено 2026-07-29)
DS_PRICE_IN, DS_PRICE_OUT = 0.435, 0.87          # deepseek-v4-pro, вход cache-miss
CL_IN, CL_CACHE_W, CL_CACHE_R, CL_OUT = 5.0, 6.25, 0.50, 25.0   # Claude Opus 5
USD_RUB = 78.698                                 # ЦБ РФ на 2026-07-29
TRANSCRIPT = Path("/root/.claude/projects/-root")

NUM = re.compile(r"\d+(?:[.,]\d+)?")
UNIT_TO_CM = {"cm": 1.0, "см": 1.0, "mm": 0.1, "мм": 0.1,
              "in": 2.54, "inch": 2.54, "inches": 2.54, "\"": 2.54, "дюйм": 2.54}


def unit_factor(unit: str, raw: str) -> float | None:
    u = (unit or "").strip().lower().strip(".")
    if u in UNIT_TO_CM:
        return UNIT_TO_CM[u]
    low = (raw or "").lower()
    for key, f in UNIT_TO_CM.items():
        if key in low:
            return f
    return None


def to_cm(dimensions, unit: str) -> tuple[str, str]:
    """→ ('Д x Ш x В' в см, исходная строка). Пусто, если трёх чисел нет."""
    if isinstance(dimensions, dict):
        vals = [dimensions.get(k) for k in ("length", "width", "height")]
        raw = " x ".join(str(v) for v in vals if v is not None)
    else:
        raw = str(dimensions or "")
    nums = [float(x.replace(",", ".")) for x in NUM.findall(raw)]
    if len(nums) < 3:
        return "", raw
    f = unit_factor(unit, raw)
    if f is None:
        return "", raw
    cm = [round(n * f, 1) for n in nums[:3]]
    return " x ".join(f"{v:g}" for v in cm), raw


def cmd_candidates(batch: Path) -> dict:
    src = batch / "extract.jsonl"
    rows, skipped = [], []
    for line in io.open(src, encoding="utf-8"):
        r = json.loads(line)
        if r.get("result") != "CANDIDATE":
            continue
        dims_cm, raw = to_cm(r.get("dimensions"), r.get("source_unit", ""))
        url = str(r.get("url") or "")
        ok = bool(dims_cm) and url.startswith("http") and bool(r.get("evidence_quote"))
        row = {
            "vendorCode": r.get("vendorCode", ""), "model": r.get("model", ""),
            "manufacturer": r.get("manufacturer", ""), "oem_code": r.get("model", ""),
            "url": url, "title": (r.get("page_title") or "")[:200],
            "dimensions_cm": dims_cm, "original_dimensions": raw,
            "original_unit": r.get("source_unit", ""),
            "dimension_type": r.get("dimension_type", ""),
            "evidence": (r.get("evidence_quote") or "")[:1000],
            "fetch_method": r.get("fetch_method", ""),
            "_structural_check": "READY_FOR_VALIDATION" if ok else "INCOMPLETE",
        }
        (rows if ok else skipped).append(row)
    out = batch / "candidate.csv"
    with io.open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CAND_COLS, delimiter=";")
        w.writeheader()
        for r in rows + skipped:
            w.writerow(r)
    return {"файл": str(out), "READY": len(rows), "INCOMPLETE": len(skipped),
            "команда_валидации":
                f"./venv/bin/python -m tools.deepseek_candidate_validator "
                f"--input {out} --output-dir {batch/'validation'} --expected-count {len(rows)}"}


def claude_usage(session_jsonl: Path) -> dict:
    """Суммарный расход Claude по стенограмме сессии (файл в контекст не тянем)."""
    a = {"input_tokens": 0, "output_tokens": 0,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    if not session_jsonl.exists():
        return a
    for line in io.open(session_jsonl, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        u = ((d.get("message") or {}).get("usage")) or {}
        for k in a:
            a[k] += int(u.get(k) or 0)
    return a


def claude_cost_usd(u: dict) -> float:
    return (u["input_tokens"] * CL_IN + u["output_tokens"] * CL_OUT
            + u["cache_creation_input_tokens"] * CL_CACHE_W
            + u["cache_read_input_tokens"] * CL_CACHE_R) / 1_000_000


def _models_index() -> dict:
    idx = {}
    with io.open(MODELS_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            idx[r["семья_мать"]] = {"oem": r["OEM_модель"], "rev": int(r["выручка_год_₽"]),
                                    "cards": int(r["карточек"]), "rank": int(r["ранг"])}
    return idx


def cmd_summary(batch: Path) -> dict:
    idx = _models_index()
    total_uncovered = sum(v["rev"] for v in idx.values())

    positions = list(csv.DictReader(io.open(batch / "positions.csv", encoding="utf-8-sig"),
                                    delimiter=";"))
    all_in_batch = {p["vendorCode"] for p in positions}
    # в партии есть все модели диапазона; «спросили» — те, для кого нашлись URL
    urls_map = json.load(io.open(batch / "urls.json", encoding="utf-8"))
    asked = {vc for vc, u in urls_map.items() if u}

    extract = [json.loads(x) for x in io.open(batch / "extract.jsonl", encoding="utf-8")] \
        if (batch / "extract.jsonl").exists() else []
    ds_tokens = 0
    tok_file = batch / "extract_summary.json"
    if tok_file.exists():
        ds_tokens += int(json.load(io.open(tok_file, encoding="utf-8"))
                         .get("deepseek_total_tokens", 0))

    conf_path = batch / "validation" / "confirmed.csv"
    confirmed = []
    if conf_path.exists():
        confirmed = list(csv.DictReader(io.open(conf_path, encoding="utf-8-sig"), delimiter=";"))
    val_tokens = 0
    vsum = batch / "validation" / "summary.json"
    if vsum.exists():
        d = json.load(io.open(vsum, encoding="utf-8"))
        val_tokens = int(d.get("total_tokens") or 0)

    conf_codes = {c.get("vendorCode", "").zfill(4) for c in confirmed}
    closed_rev = sum(idx[c]["rev"] for c in conf_codes if c in idx)

    # накопительно по всем партиям
    cum_codes = set()
    for b in sorted(HUNT_DIR.glob("batch_*")):
        p = b / "validation" / "confirmed.csv"
        if p.exists():
            cum_codes |= {r.get("vendorCode", "").zfill(4)
                          for r in csv.DictReader(io.open(p, encoding="utf-8-sig"), delimiter=";")}
    cum_rev = sum(idx[c]["rev"] for c in cum_codes if c in idx)

    return {
        "партия": batch.name,
        "моделей_в_партии": len(all_in_batch),
        "нашлись_страницы": len(asked),
        "без_страниц": sorted(all_in_batch - asked),
        "кандидатов_извлечено": sum(1 for r in extract if r.get("result") == "CANDIDATE"),
        "не_найдено": sum(1 for r in extract if r.get("result") == "NOT_FOUND"),
        "спорно": sum(1 for r in extract if r.get("result") == "AMBIGUOUS"),
        "источник_недоступен": sum(1 for r in extract
                                   if r.get("result") == "SOURCE_UNAVAILABLE"),
        "CONFIRMED": len(confirmed),
        "закрыто_выручки_партия_₽": closed_rev,
        "закрыто_выручки_накопительно_₽": cum_rev,
        "доля_непокрытой_выручки_накопительно_%": round(100 * cum_rev / total_uncovered, 2),
        "непокрытая_выручка_всего_₽": total_uncovered,
        "токены_deepseek_извлечение": ds_tokens,
        "токены_deepseek_валидация": val_tokens,
        # почти весь объём ДипСика — вход (текст страницы), ответ ~100–300 токенов,
        # поэтому считаем по входной цене, погрешность в пределах 1 %
        "deepseek_$": round((ds_tokens + val_tokens) * DS_PRICE_IN / 1_000_000, 3),
        "deepseek_₽": round((ds_tokens + val_tokens) * DS_PRICE_IN / 1_000_000 * USD_RUB, 1),
    }


def cmd_claude_cost(session_id: str, baseline: str | None) -> dict:
    cur = claude_usage(TRANSCRIPT / f"{session_id}.jsonl")
    base = json.load(io.open(baseline, encoding="utf-8")) if baseline else \
        {k: 0 for k in cur}
    delta = {k: cur[k] - int(base.get(k, 0)) for k in cur}
    return {"сессия_всего_токенов": cur, "сессия_$": round(claude_cost_usd(cur), 2),
            "сессия_₽": round(claude_cost_usd(cur) * USD_RUB, 1),
            "с_момента_отметки_токенов": delta,
            "с_момента_отметки_$": round(claude_cost_usd(delta), 2),
            "с_момента_отметки_₽": round(claude_cost_usd(delta) * USD_RUB, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["candidates", "summary", "claude-cost"])
    ap.add_argument("--batch")
    ap.add_argument("--session")
    ap.add_argument("--baseline")
    a = ap.parse_args()
    if a.cmd == "claude-cost":
        res = cmd_claude_cost(a.session, a.baseline)
    else:
        batch = Path(a.batch)
        res = cmd_candidates(batch) if a.cmd == "candidates" else cmd_summary(batch)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()

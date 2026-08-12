# поток: fin
"""Аудит строк Финансы→Баланс Ozon: состав каждого блока против реестра.

Три проверки за один прогон:
  1. НОВЫЙ КОД — тип операции или услуга, которых нет в `ops/ozon_lines_registry.py`.
     Такой код молча уезжает в фолбэк (`other` для операций, `delivery` для услуг) и тихо
     перекашивает отчёт — ровно так «Досрочная выплата» попала в «Компенсации» (июль-2026).
  2. СМЕНА СТРОКИ — код есть в реестре, но текущие правила `reports/ozon_mp_report.py`
     кладут его в другую строку, чем зафиксировано. Ловит случайную правку классификатора.
  3. СВЕРКА С ЛК — если для (аккаунт, месяц) есть эталон в `LK_REF`, сравниваем 10 строк.

Прогон:  PYTHONPATH=/opt/mp-analytics ./venv/bin/python ops/ozon_lines_audit.py \
             --since 2026-01 --until 2026-07 [--out docs/reports/имя.md]
Код возврата 1, если нашлись новые коды или сменившие строку (для cron/CI).
"""
import argparse
import datetime
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import db                                          # noqa: E402
from ops.ozon_lines_registry import RESID_OPS, SERVICES, LK_REF   # noqa: E402
from reports import ozon_mp_report as R                      # noqa: E402

ACCOUNTS = ("oz_acc1", "oz_acc2")
LINES = ("sales", "returns", "commission", "delivery", "partners", "fbo",
         "promo", "penalty", "compensation", "other")


def _months(since, until):
    y, m = int(since[:4]), int(since[5:7])
    ey, em = int(until[:4]), int(until[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def scan(since, until):
    """Состав строк из сырья: {(строка, код): [сумма, штук]} + множества встреченных кодов."""
    d1 = f"{since}-01"
    ey, em = int(until[:4]), int(until[5:7])
    d2 = f"{ey + 1}-01-01" if em == 12 else f"{ey}-{em + 1:02d}-01"
    rows = db.query(
        """SELECT payload FROM raw_ozon_transaction
             WHERE (payload->>'operation_date')::date>=%s
               AND (payload->>'operation_date')::date<%s""", (d1, d2))
    comp = defaultdict(lambda: [0.0, 0])
    seen_ops, seen_svc = set(), set()
    for r in rows:
        p = r["payload"]
        am = float(p.get("amount") or 0)
        acc = float(p.get("accruals_for_sale") or 0)
        cm = float(p.get("sale_commission") or 0)
        ss = 0.0
        for s in (p.get("services") or []):
            name = s.get("name", "")
            price = float(s.get("price") or 0)
            ss += price
            seen_svc.add(name)
            k = (R._svc_line(name), name)
            comp[k][0] += -price
            comp[k][1] += 1
        res = am - acc - cm - ss
        ot = p.get("operation_type", "")
        if abs(res) > 0.005:
            seen_ops.add(ot)
            k = (R._resid_line(ot), ot)
            comp[k][0] += -res
            comp[k][1] += 1
    return comp, seen_ops, seen_svc


def audit(since, until):
    comp, seen_ops, seen_svc = scan(since, until)
    new, moved = [], []
    for code in sorted(seen_ops):
        if code not in RESID_OPS:
            new.append(("операция", code, R._resid_line(code)))
        elif RESID_OPS[code][0] != R._resid_line(code):
            moved.append(("операция", code, RESID_OPS[code][0], R._resid_line(code)))
    for code in sorted(seen_svc):
        if code not in SERVICES:
            new.append(("услуга", code, R._svc_line(code)))
        elif SERVICES[code][0] != R._svc_line(code):
            moved.append(("услуга", code, SERVICES[code][0], R._svc_line(code)))
    lk = []
    for acc in ACCOUNTS:
        for y, m in _months(since, until):
            ref = LK_REF.get((acc, y, m))
            if not ref:
                continue
            b = R.balance(acc, y, m)
            # Продажи/Возвраты/Вознаграждение в отчёте берутся не из транзакций, а из Отчёта
            # о реализации (правило 8 CLAUDE.md) — сверять с ЛК надо тем же источником.
            rs = R.realiz_sales(acc, y, m)
            if rs:
                b = dict(b)
                b["sales"], b["returns"], b["commission"] = rs
            for line in LINES:
                delta = round(b[line]) - ref[line]
                if delta:
                    lk.append((acc, f"{y}-{m:02d}", line, ref[line], round(b[line]), delta))
    return comp, new, moved, lk


def report(comp, new, moved, lk, since, until, out_path):
    L = [f"# Аудит строк Финансы→Баланс Ozon, {since}…{until}", "",
         f"Прогон {datetime.date.today().isoformat()}, `ops/ozon_lines_audit.py`.", ""]
    L.append("## Новые коды (нет в реестре — уехали в фолбэк)")
    L += ["", "нет" if not new else ""] if not new else [""]
    for kind, code, got in new:
        L.append(f"- **{code}** ({kind}) → сейчас падает в `{got}`")
    L += ["", "## Сменили строку относительно реестра"]
    if not moved:
        L.append("нет")
    for kind, code, was, got in moved:
        L.append(f"- **{code}** ({kind}): реестр `{was}` → правила дают `{got}`")
    L += ["", "## Сверка с ЛК (эталонные месяцы)"]
    if not lk:
        L.append("расхождений нет")
    for acc, key, line, ref, got, delta in lk:
        L.append(f"- {acc} {key} `{line}`: ЛК {ref:,} / наше {got:,} — дельта {delta:+,}")
    L += ["", "## Состав строк (сумма за период, ₽)", ""]
    by_line = defaultdict(list)
    for (line, code), (amount, cnt) in comp.items():
        by_line[line].append((amount, cnt, code))
    for line in LINES:
        if line not in by_line:
            continue
        L.append(f"### {line}")
        L.append("")
        L.append("| код | имя в ЛК | сумма | строк |")
        L.append("|---|---|---:|---:|")
        for amount, cnt, code in sorted(by_line[line], key=lambda x: -abs(x[0])):
            ru = (RESID_OPS.get(code) or SERVICES.get(code) or ("", "—"))[1] or "—"
            L.append(f"| `{code}` | {ru} | {amount:,.2f} | {cnt} |")
        L.append("")
    pathlib.Path(out_path).write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01")
    ap.add_argument("--until", default=datetime.date.today().strftime("%Y-%m"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or f"docs/reports/ozon_mp_lines_audit_{datetime.date.today().isoformat()}.md"
    comp, new, moved, lk = audit(a.since, a.until)
    report(comp, new, moved, lk, a.since, a.until, out)
    print(f"[oz-lines] {a.since}…{a.until}: новых кодов {len(new)}, сменили строку {len(moved)}, "
          f"расхождений с ЛК {len(lk)} → {out}")
    for kind, code, got in new:
        print(f"  НОВЫЙ {kind}: {code} → {got}")
    for kind, code, was, got in moved:
        print(f"  СМЕНА {kind}: {code}: {was} → {got}")
    for acc, key, line, ref, got, delta in lk:
        print(f"  ЛК {acc} {key} {line}: {ref:,} vs {got:,} ({delta:+,})")
    return 1 if (new or moved) else 0


if __name__ == "__main__":
    sys.exit(main())

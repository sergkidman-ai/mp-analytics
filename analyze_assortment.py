"""analyze_assortment.py — полная картина по Джему: насколько откатились ПО ВСЕМУ ассортименту.

Тянет отчёт search-report за текущую неделю vs МАЙСКАЯ база (до роста цен) по всем товарам,
считает распределение просадки (позиция/заказы/видимость) и топ-падения. → docs/jam_assortment.txt

Запуск:  ./venv/bin/python analyze_assortment.py
"""
import os
import sys
import time
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
URL = "https://seller-analytics-api.wildberries.ru/api/v2/search-report/report"
OUT = BASE_DIR / "docs" / "jam_assortment.txt"
CUR = {"start": "2026-06-16", "end": "2026-06-22"}     # последняя полная неделя
MAY = {"start": "2026-05-19", "end": "2026-05-25"}     # майская база (до роста цен)


def _cd(o, k):
    v = o.get(k) or {}
    return v.get("current"), v.get("dynamics")


def fetch_all():
    tok = os.getenv("WB_TOKEN_ACC1") or os.getenv("WB_TOKEN")
    H = {"Authorization": tok, "Content-Type": "application/json"}
    LIM, offset, items = 100, 0, []
    summary = None
    while True:
        body = {"currentPeriod": CUR, "pastPeriod": MAY, "nmIds": [],
                "positionCluster": "all", "orderBy": {"field": "openCard", "mode": "desc"},
                "limit": LIM, "offset": offset}
        for _ in range(6):
            r = requests.post(URL, headers=H, json=body, timeout=120)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
                continue
            r.raise_for_status()
            break
        data = r.json().get("data") or {}
        if summary is None:
            summary = data
        page = []
        for g in data.get("groups") or []:
            page += g.get("items") or []
        items += page
        print(f"  offset {offset}: +{len(page)} (всего {len(items)})", flush=True)
        if len(page) < LIM:
            break
        offset += LIM
        time.sleep(5)
    return summary, items


def main():
    print(f"Джем весь ассортимент: {CUR['start']}..{CUR['end']} vs майская база {MAY['start']}..{MAY['end']}", flush=True)
    summary, items = fetch_all()
    rows = []
    for it in items:
        ap = _cd(it, "avgPosition")
        od = _cd(it, "orders")
        vis = _cd(it, "visibility")
        oc = _cd(it, "openCard")
        rows.append({
            "nm": it.get("nmId"), "name": it.get("name"), "adv": it.get("isAdvertised"),
            "price": (it.get("price") or {}).get("minPrice"),
            "pos": ap[0], "pos_dyn": ap[1], "orders": od[0], "orders_dyn": od[1],
            "vis": vis[0], "vis_dyn": vis[1], "open": oc[0], "open_dyn": oc[1],
        })

    # сохраняем в БД для глобальной сегментации (джойн с выручкой/себестом)
    db.upsert("wb_jam_may", [{
        "account": "wb_acc1", "nm_id": r["nm"], "name": r["name"], "is_advertised": r["adv"],
        "price": r["price"], "pos": r["pos"], "pos_dyn": r["pos_dyn"],
        "orders": r["orders"], "orders_dyn": r["orders_dyn"], "open": r["open"], "open_dyn": r["open_dyn"],
        "visibility": r["vis"], "vis_dyn": r["vis_dyn"],
    } for r in rows if r["nm"]], conflict_cols=["account", "nm_id"])

    n = len(rows)
    # распределения (dynamics: позиция в пунктах +хуже/−лучше; заказы/показы % к маю)
    pos_worse = [r for r in rows if (r["pos_dyn"] or 0) > 0]
    pos_better = [r for r in rows if (r["pos_dyn"] or 0) < 0]
    fell_out = [r for r in rows if (r["pos"] in (0, None)) and (r["pos_dyn"] or 0) > 0]
    ord_down = [r for r in rows if (r["orders_dyn"] or 0) < 0]
    ord_zeroed = [r for r in rows if (r["orders_dyn"] or 0) <= -100]
    open_down = [r for r in rows if (r["open_dyn"] or 0) < 0]
    vis_down = [r for r in rows if (r["vis_dyn"] or 0) < 0]
    adv = [r for r in rows if r["adv"]]

    ci = (summary or {}).get("commonInfo") or {}
    pi = (summary or {}).get("positionInfo") or {}
    vi = (summary or {}).get("visibilityInfo") or {}

    def line(f, r):
        nm, name = r["nm"], (r["name"] or "")[:36]
        pos = str(r["pos"]) if r["pos"] is not None else "—"
        pd = r["pos_dyn"]
        pda = ("▲+" + str(pd) if (pd or 0) > 0 else ("▼" + str(pd) if pd else "0"))
        od = r["orders_dyn"]
        f.write(f"  {nm:>11} {pos:>4} {pda:>7} {str(r['orders'] or 0):>5} "
                f"{(str(od)+'%') if od is not None else '—':>7} {str(r['open'] or 0):>7} {str(r['price'] or '—'):>7}  {name}\n")

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("ДЖЕМ — ВЕСЬ АССОРТИМЕНТ: насколько откатились (тек. неделя vs майская база, до роста цен)\n")
        f.write(f"Период: {CUR['start']}..{CUR['end']}  vs  {MAY['start']}..{MAY['end']}\n")
        f.write("Позиция: меньше=выше. Δпоз ▲ = упали ниже (плохо), ▼ = поднялись. Δзак% — заказы к маю.\n")
        f.write("=" * 104 + "\n\n")
        f.write("1. ОБЩАЯ КАРТИНА (сводка кабинета)\n")
        f.write(f"   Товаров в отчёте: {n}\n")
        f.write(f"   Средняя позиция: {_cd(pi,'average')[0]} (Δ {_cd(pi,'average')[1]} к маю)   "
                f"медианная: {_cd(pi,'median')[0]} (Δ {_cd(pi,'median')[1]})\n")
        f.write(f"   В топ-100: {((pi.get('clusters') or {}).get('firstHundred') or {}).get('current')} "
                f"(Δ {((pi.get('clusters') or {}).get('firstHundred') or {}).get('dynamics')} к маю)\n")
        f.write(f"   Видимость: {_cd(vi,'visibility')[0]} (Δ {_cd(vi,'visibility')[1]})   "
                f"показы карточек: {_cd(vi,'openCard')[0]} (Δ {_cd(vi,'openCard')[1]})\n\n")
        f.write("2. РАСПРЕДЕЛЕНИЕ ПРОСАДКИ ПО SKU (из " + str(n) + ")\n")
        f.write(f"   Позиция УПАЛА ниже:        {len(pos_worse):>4}  ({round(len(pos_worse)/n*100)}%)\n")
        f.write(f"   Позиция поднялась:         {len(pos_better):>4}  ({round(len(pos_better)/n*100)}%)\n")
        f.write(f"   Выпали из выдачи (поз 0):  {len(fell_out):>4}  ({round(len(fell_out)/n*100)}%)\n")
        f.write(f"   Заказы упали к маю:        {len(ord_down):>4}  ({round(len(ord_down)/n*100)}%)\n")
        f.write(f"   Заказы обнулились (−100%): {len(ord_zeroed):>4}  ({round(len(ord_zeroed)/n*100)}%)\n")
        f.write(f"   Показы карточек упали:     {len(open_down):>4}  ({round(len(open_down)/n*100)}%)\n")
        f.write(f"   Видимость упала:           {len(vis_down):>4}  ({round(len(vis_down)/n*100)}%)\n")
        f.write(f"   На рекламе:                {len(adv):>4}  ({round(len(adv)/n*100)}%)\n\n")

        f.write("3. ТОП-40 ПАДЕНИЙ ПОЗИЦИИ (трафик есть: показы ≥5)\n")
        f.write(f"  {'nmID':>11} {'поз':>4} {'Δпоз':>7} {'зак':>5} {'Δзак%':>7} {'показы':>7} {'цена':>7}  товар\n")
        f.write("  " + "-" * 98 + "\n")
        top = sorted([r for r in rows if (r["open"] or 0) >= 5], key=lambda r: -(r["pos_dyn"] or -999))[:40]
        for r in top:
            line(f, r)

        f.write("\n4. ВЫПАВШИЕ ИЗ ВЫДАЧИ (поз 0) С МАЙСКИМ ТРАФИКОМ — кандидаты на возврат\n")
        f.write(f"  {'nmID':>11} {'поз':>4} {'Δпоз':>7} {'зак':>5} {'Δзак%':>7} {'показы':>7} {'цена':>7}  товар\n")
        f.write("  " + "-" * 98 + "\n")
        for r in sorted(fell_out, key=lambda r: -(r["price"] or 0))[:40]:
            line(f, r)
        f.write("\n— конец —\n")
    print(f"Готово: {OUT} | товаров {n} | поз.упала {len(pos_worse)} | выпали {len(fell_out)} | заказы вниз {len(ord_down)}")


if __name__ == "__main__":
    main()

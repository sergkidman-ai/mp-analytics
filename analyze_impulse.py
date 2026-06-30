"""analyze_impulse.py — свод «куда бить импульсом» по выпавшим героям ВБ.

Соединяет три метода в одну картину по nmId:
  • wb_ad_nm        — реклама на уровне товара (cpc=факт.ставка, расход, заказы, ДРР) внутри кампаний
  • wb_search_report — позиция в выдаче + динамика (Джем)
  • wb_search_text   — лучший поисковый запрос товара (частота, позиция)
  • docs/rollback_digital.txt — текущая цена vs майский возврат

Итог → docs/impulse_heroes.txt (less). Только Цифровой (Джем/реклама там).

Запуск:  ./venv/bin/python analyze_impulse.py
"""
import re
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

OUT = BASE_DIR / "docs" / "impulse_heroes.txt"
ACC = "wb_acc1"


def rollback_map():
    """nmID -> (тек_цена, цена_возврата, выросла_%) из откат-файла Цифрового."""
    p = BASE_DIR / "docs" / "rollback_digital.txt"
    m = {}
    if not p.exists():
        return m
    for line in p.read_text(encoding="utf-8").splitlines():
        # формат: nmID  май  тек.цена  возврат  выросла  снизить  чистаяМай  товар
        g = re.match(r"\s*(\d{6,})\s+\d+\s+(\d+)\s+(\d+)\s+([+\-]\d+)%", line)
        if g:
            m[int(g.group(1))] = (int(g.group(2)), int(g.group(3)), g.group(4))
    return m


def w(f, s=""):
    f.write(s + "\n")


def main():
    roll = rollback_map()
    nmids = list(roll.keys())

    # реклама на уровне товара (суммарно по всем кампаниям) за последний период
    ad = {}
    for r in db.query("""
        SELECT nm_id,
               sum(spend) spend, sum(orders) orders, sum(clicks) clicks, sum(revenue) revenue,
               round(sum(spend)/NULLIF(sum(clicks),0),2) cpc,
               round(sum(spend)/NULLIF(sum(revenue),0)*100,1) drr,
               count(DISTINCT advert_id) camps
        FROM wb_ad_nm WHERE account=%s GROUP BY nm_id
    """, (ACC,)):
        ad[r["nm_id"]] = r

    # позиции (Джем)
    pos = {r["nm_id"]: r for r in db.query(
        "SELECT nm_id, name, avg_position, avg_position_dyn, orders, orders_dyn, visibility, open_card "
        "FROM wb_search_report WHERE account=%s", (ACC,))}

    # лучший запрос по товару (макс. частота)
    besttxt = {}
    for r in db.query("""
        SELECT DISTINCT ON (nm_id) nm_id, text, week_frequency, avg_position, avg_position_dyn
        FROM wb_search_text WHERE account=%s
        ORDER BY nm_id, week_frequency DESC NULLS LAST
    """, (ACC,)):
        besttxt[r["nm_id"]] = r

    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        w(f, "ИМПУЛЬС ПО ВЫПАВШИМ ГЕРОЯМ — ЦИФРОВОЙ (свод: реклама×позиции×цена)")
        w(f, "cpc — факт. ставка за клик по товару; ДРР — расход/выручка рекламы; поз — позиция в выдаче (меньше=выше).")
        w(f, "Δпоз: ▼ поднялись / ▲ упали. Цена ↑% — на сколько подняли с мая; возврат — майский уровень.")
        w(f, "=" * 110)

        # ранжируем: сначала те, у кого есть и реклама, и позиция, по расходу рекламы
        def sortkey(nm):
            a = ad.get(nm, {})
            return -(a.get("spend") or 0)
        rows = sorted(nmids, key=sortkey)

        w(f, f"\n{'nmID':>11} {'поз':>4} {'Δпоз':>6} | {'cpc':>5} {'расх':>6} {'зак':>4} {'ДРР':>6} {'камп':>4} | "
             f"{'цена':>5} {'возвр':>5} {'↑%':>5} | запрос(частота) / товар")
        w(f, "-" * 110)
        shown = 0
        for nm in rows:
            a, p, t = ad.get(nm), pos.get(nm), besttxt.get(nm)
            cur, ret, grew = roll.get(nm, (None, None, ""))
            if not a and not p:
                continue
            shown += 1
            poz = str(p["avg_position"]) if p and p.get("avg_position") is not None else "—"
            dpoz = ""
            if p and p.get("avg_position_dyn") is not None:
                d = p["avg_position_dyn"]
                dpoz = ("▼" if d < 0 else "▲" if d > 0 else "") + (str(d) if d else "0")
            cpc = f"{a['cpc']:.0f}" if a and a.get("cpc") else "—"
            sp = f"{a['spend']:.0f}" if a and a.get("spend") else "—"
            zak = str(a["orders"]) if a and a.get("orders") is not None else "—"
            drr = f"{a['drr']:.0f}%" if a and a.get("drr") is not None else "—"
            camps = str(a["camps"]) if a else "—"
            q = ""
            if t:
                q = f"{(t['text'] or '')[:34]} ({t.get('week_frequency') or 0}/нед, поз {t.get('avg_position')})"
            name = (p["name"] if p else (t["text"] if t else "")) or ""
            w(f, f"{nm:>11} {poz:>4} {dpoz:>6} | {cpc:>5} {sp:>6} {zak:>4} {drr:>6} {camps:>4} | "
                 f"{str(cur or '—'):>5} {str(ret or '—'):>5} {grew:>5} | {q}  | {name[:30]}")
        w(f, f"\nвсего героев с рекламой/позицией: {shown} из {len(nmids)} в списке отката")

        # сводка рекламы по аккаунту
        tot = db.query("SELECT sum(spend) s, sum(revenue) r, sum(orders) o, count(*) rows, "
                       "count(DISTINCT nm_id) nms, count(DISTINCT advert_id) camps FROM wb_ad_nm WHERE account=%s", (ACC,))
        if tot and tot[0]["rows"]:
            tt = tot[0]
            drr = round((tt["s"] or 0) / (tt["r"] or 1) * 100, 1)
            w(f, "\n" + "=" * 110)
            w(f, f"РЕКЛАМА ВСЕГО (товар×кампания): {tt['rows']} строк, {tt['nms']} товаров, {tt['camps']} кампаний")
            w(f, f"  расход {tt['s']:,.0f} ₽ | выручка {tt['r']:,.0f} ₽ | заказы {tt['o']} | ДРР {drr}%")
        w(f, "\n— конец —")
    print(f"Готово: {OUT}  (героев из отката: {len(nmids)})")


if __name__ == "__main__":
    main()

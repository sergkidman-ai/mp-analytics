# поток: prc — автозаведение карточек МС по строкам новинок, где сверка сошлась вся.
"""Строку новинки, у которой сверка с товаром поставщика и каталогом ТК дала ВСЕ ШЕСТЬ
галочек, заводим в МойСклад без человека; всё остальное — человеку на вкладку «Новинки».

Заказ Сергея 31.08.2026. Шесть галочек — `model_ok, kind_ok, brand_ok, color_ok,
resource_ok, chip_ok` (`prices/catalog.py::compare`).

Почему одних галочек мало. Галочки говорят «эта строка прайса про тот же товар, что карточка
МС», но не «карточка выйдет годной». Годность решает уже собранный черновик: пустой вес или
пустой Code128 — карточка нарушает правило 11 обязательных полей, и такую мы не заводим
(решение Сергея: отдать человеку). Плюс список стоп-замечаний ниже: любое из них означает
«данные расходятся, и выбор между ними — не машинное дело».

Автомат ничего не переписывает: `create_in_ms` существующую карточку только пропускает.
Боевой режим по расписанию включается списком `AUTO_APPLY` — пустым до отдельной команды.
"""
import argparse
import datetime as dt

from core.db import query
from prices import enter_add, ms_import
from prices.catalog import IN_STOCK, NOVELTY_STOCK
from prices.profiles import get_identity
from prices.supplier_group import own_ids

SIX = ("model_ok", "kind_ok", "brand_ok", "color_ok", "resource_ok", "chip_ok")

# Поставщики, у которых автозаведение работает БОЕВЫМ ходом внутри разбора прайса. Пусто до
# отдельной команды Сергея: до неё каждый прогон — сухой отчёт. Включение = правка этого
# кортежа, кода это не трогает.
AUTO_APPLY = ()

# Замечания черновика, при которых карточку заводит человек. Каждое из них — расхождение
# между источниками (прайс / карточки МС / каталог ТК / Озон) либо прямая просьба проверить.
# «сверить не с чем» тоже здесь: вес, который не подтверждён ни прайсом, ни карточками МС,
# в каталоге ТК бывает с опечаткой (код 6882 = 9001 г при 900 г у соседа), а завышенный вес
# на площадке стоит денег в логистике.
STOP = ("уже есть в МС", "нет живой карточки МС", "модель в названии поставщика",
        "ресурс в названии поставщика", "вес ТК", "вес поставщика", "вес у карточек МС",
        "сверить не с чем", "проверить перед созданием", "группа у карточек МС",
        "штрихкод у карточек МС", "нет цены в прайсе")


def pick(supplier_key, limit=None, any_stock=False):
    """Строки, где сверка дала все шесть галочек и кандидат один. Ничего не пишет.

    `any_stock` — снять требование остатка. Только для просмотра: строку без остатка заводить
    не под что (то же правило, что на вкладке «Новинки»), в бой она не идёт.
    """
    six = " AND ".join(f"c.{f} IS TRUE" for f in SIX)
    rival = " AND ".join(f"c2.{f} IS TRUE" for f in SIX)
    rows = query(f"""
        SELECT n.id, n.article, n.name, c.ms_code, c.ms_id, c.ms_name, c.external_code,
               coalesce(s.qty, 0) qty
          FROM prc_novelty n {NOVELTY_STOCK}
          JOIN prc_novelty_candidate c ON c.novelty_id = n.id AND c.rank = 1
         WHERE n.supplier_key = %s AND n.decision = 'pending' AND {six}
           -- Кандидат без кода карточки записать нечем: `ms_code` — ключ строки новинки.
           AND c.ms_code IS NOT NULL
           AND ({'TRUE' if any_stock else IN_STOCK})
           -- Второй кандидат с теми же шестью галочками, но ДРУГИМ внешним кодом — это два
           -- разных товара, одинаково похожих на строку. Выбор между ними делает человек.
           AND NOT EXISTS (SELECT 1 FROM prc_novelty_candidate c2
                            WHERE c2.novelty_id = n.id AND c2.rank <> 1 AND {rival}
                              AND c2.external_code IS DISTINCT FROM c.external_code)
         ORDER BY n.id""", (supplier_key,))
    return rows[:limit] if limit else rows


def screen(supplier_key, cand, log=print):
    """Черновики карточек по отобранным строкам -> (годные записи, отвод с причиной).

    Черновик собирает `ms_import.build` — тот же, что стоит за кнопкой «➕ В МС», поэтому
    автомат и человек видят одну и ту же карточку и одни и те же замечания.
    """
    if not cand:
        return [], [], {}
    ids = [c["id"] for c in cand]
    records, notes = ms_import.build(supplier_key, ("pending",), ids=ids,
                                     ms_codes={c["id"]: c["ms_code"] for c in cand})
    flags = {nid: fl for nid, _article, fl in notes}
    by_id = {c["id"]: c for c in cand}
    good, held = [], []
    for rec in records:
        row = by_id.get(rec.get("_novelty_id"))
        if not row:
            continue
        fl = flags.get(rec["_novelty_id"], [])
        why = None
        if not rec["Вес"]:
            why = "нет веса — заполнить руками"
        elif not rec["Штрихкод Code128"]:
            why = "нет штрихкода Code128 — заполнить руками"
        else:
            stop = [f for f in fl if any(w in f for w in STOP)]
            if stop:
                why = stop[0]
        (held if why else good).append({**row, "rec": rec, "why": why, "flags": fl})
    # Строка, до записи не дожившая (черновик не собрался — например, внешнего кода нет в МС):
    # молча терять её нельзя, иначе отчёт обещает разбор, которого не было.
    seen = {g["id"] for g in good} | {h["id"] for h in held}
    for c in cand:
        if c["id"] not in seen:
            held.append({**c, "rec": None, "flags": flags.get(c["id"], []),
                         "why": (flags.get(c["id"]) or ["черновик не собрался"])[0]})
    return good, held, flags


def run(supplier_key, dry=True, apply_cap=20, ids=None, any_stock=False, log=print):
    """Отобрать, собрать, (в боевом режиме) завести и добрать в оприходование.

    -> сводка словарём. В сухом режиме в МС и в БД не пишется ничего.
    """
    profile = get_identity(supplier_key)
    cand = pick(profile.key, any_stock=any_stock)
    if ids:
        cand = [c for c in cand if c["id"] in set(ids)]
    good, held, _ = screen(profile.key, cand, log=log)
    out = {"supplier": profile.key, "candidates": len(cand), "ready": len(good),
           "held": len(held), "created": 0, "added": 0, "dry": dry, "report": None}

    capped = good[:apply_cap] if not dry else good
    if not dry and capped:
        records = [g["rec"] for g in capped]
        created, skipped = ms_import.create_in_ms(records, profile.supplier_ids[0], dry=False,
                                                  log=log, own=own_ids(profile))
        out["created"] = ms_import.mark_created(records, created)
        out["skipped"] = len(skipped)
        if created:
            # Карточка без позиции в приходе — половина работы: остаток поставщика на неё не
            # ляжет, и человеку придётся вспоминать про второй шаг. Добираем сразу.
            lines, plan, skip, where = enter_add.add_cards(
                enter_add.by_ids([ms_id for ms_id, _c, _a in created], log=log), dry=False, log=log)
            out["added"] = len(plan)
            out["not_added"] = [(s["code"], s["why"]) for s in skip]
            for g in capped:
                g["doc"] = where.get(g["rec"]["Код"])
    out["report"] = _report(profile.key, cand, good, held, capped, dry, out)
    for line in (f"поставщик {profile.key}: 6/6 и один кандидат — {len(cand)}",
                 f"  годных черновиков: {len(good)}"
                 + (f", завёл {out['created']}, добрал {out['added']}" if not dry else ""),
                 f"  отдано человеку: {len(held)}",
                 f"  отчёт: {out['report']}" + ("  (сухой прогон)" if dry else "")):
        log(line)
    return out


def _report(key, cand, good, held, capped, dry, out):
    day = f"{dt.date.today():%Y-%m-%d}"
    # Ключ поставщика в имени: прогон идёт по одному поставщику, и общее имя
    # затирало бы отчёт предыдущего в тот же день.
    path = f"/opt/mp-analytics/docs/reports/prc_autocard_{key}_{day}.md"
    head = "СУХОЙ ПРОГОН — в МойСклад не записано ничего" if dry else "БОЕВОЙ ПРОГОН"
    lines = [f"# Автозаведение карточек — {key}, {day}", "", head, "",
             f"Строк со всеми шестью галочками и одним кандидатом: {len(cand)}; "
             f"черновик годен: {len(good)}; отдано человеку: {len(held)}"
             + ("" if dry else f"; заведено: {out['created']}; "
                               f"добрано в оприходование: {out['added']}"), ""]
    lines += [f"## {'Завёл бы' if dry else 'Завёл'} ({len(capped)})", "",
              "| строка | артикул поставщика | код карточки | вес | Code128 | документ | наименование |",
              "|---|---|---|---|---|---|---|"]
    for g in capped:
        r = g["rec"]
        lines.append(f"| {g['id']} | {g['article']} | {r['Код']} | {r['Вес']} | "
                     f"{r['Штрихкод Code128']} | {g.get('doc') or '—'} | {r['Наименование'][:60]} |")
    if len(good) > len(capped):
        lines += ["", f"Сверх лимита прогона ({len(good) - len(capped)} шт.) — уедут следующим."]
    lines += ["", f"## Отдано человеку ({len(held)})", "",
              "| строка | артикул поставщика | код кандидата | причина |", "|---|---|---|---|"]
    for h in sorted(held, key=lambda x: (x["why"] or "", x["id"])):
        lines.append(f"| {h['id']} | {h['article']} | {h['ms_code']} | {h['why']} |")
    if not dry and out.get("not_added"):
        lines += ["", "## Карточка создана, а позиции в приходе нет", "",
                  "| код | причина |", "|---|---|"]
        lines += [f"| {c} | {w} |" for c, w in out["not_added"]]
    open(path, "w").write("\n".join(lines) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser(description="автозаведение карточек по строкам новинок 6/6")
    ap.add_argument("supplier", help="ключ поставщика (sakura, colortek, …)")
    ap.add_argument("--apply", action="store_true",
                    help="боевой режим: создать карточки в МС и добрать в оприходование")
    ap.add_argument("--ids", help="только эти строки новинок, через запятую")
    ap.add_argument("--cap", type=int, default=20, help="сколько карточек за прогон (боевой)")
    ap.add_argument("--any-stock", action="store_true",
                    help="показать и строки без остатка (только просмотр)")
    a = ap.parse_args()
    run(a.supplier, dry=not a.apply, apply_cap=a.cap, any_stock=a.any_stock,
        ids=[int(x) for x in a.ids.split(",")] if a.ids else None)


if __name__ == "__main__":
    main()          # .env читает core/db при импорте — отдельная загрузка не нужна

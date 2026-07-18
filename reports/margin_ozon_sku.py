"""reports/margin_ozon_sku.py — витрина маржи Ozon по SKU: деньги Ozon − COGS из МС.

Деньги: raw_ozon_transaction, разложенные categorize_operation (без двойного счёта).
SKU: items[].sku операции (78% операций несут товары; кампанийная реклама/подписка/
эквайринг без items — это ОВЕРХЕД, не привязывается к SKU, показывается отдельно).

COGS по тому же принципу, что у ВБ (margin_by_sku), но ключ озоновский:
  posting_number → первые 2 сегмента `order_id-shipment` → FIFO-кэш ms_demand_cogs
  (report/stock/byoperation на moment отгрузки, агенты «Покупатель Озон» и «Озон Экспресс»).
  FBO (~3.2%) отгрузки в МС НЕ имеет → COGS не находится (как WB-FBO) — в покрытии видно.

Мульти-SKU отправление: деньги и COGS делятся поровну между SKU отправления (большинство
отправлений односоставные; помечено как допущение v1).

Сторно COGS возвратов в продаваемый сток: товар, вернувшийся в сток (склад ∉ {Брак, Озон}), при
перепродаже спишет себест повторно → реверсим COGS исходной продажи в МЕСЯЦЕ возврата. Возврат
порождает денежную оп (ClientReturnAgentOperation, accr<0) и товарную (OperationReturnGoodsFBSofRMS);
обе несут items[] → берём MAX штук на SKU (одна физед.), кап по МС-бюджету sellable (ms_return_cogs,
demand.name=posting). Себест/шт = cpu_global (всеисторический, продажа обычно в другом месяце).
Гейт fail-closed: постинг без salesreturn или в Брак/Озон → бюджет 0 → без сторно.

net = Σ(категории op) − COGS. Деньги — якорь. Пишет в margin_by_sku (platform='ozon').
Запуск:  ./venv/bin/python reports/margin_ozon_sku.py [2026-06-01] [2026-06-30] [oz_acc1]
"""
import os
import sys
import time
import pathlib
import datetime
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.ozon import categorize_operation, CATEGORIES, fetch_product_offer_map  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip",
     "Accept": "application/json;charset=utf-8"}
ACC_ORG = {"oz_acc1": 'ООО "ЦИФРОВОЙ КВАДРАТ"', "oz_acc2": 'ООО "ДИСКВЭР"'}


def _ms(path, params=None):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=60)
    time.sleep(0.2)
    return r.json()


def _href_id(href):
    return href.rstrip("/").split("/")[-1]


def norm_posting(p):
    """Ключ связки = первые 2 сегмента posting_number (order_id-shipment)."""
    return "-".join((p or "").split("-")[:2])


def _agent_href(name="Покупатель Озон"):
    j = _ms("entity/counterparty", {"search": name, "limit": 50})
    for r in j.get("rows", []):
        if r["name"].lower() == name.lower():
            return r["meta"]["href"], r["name"]
    raise RuntimeError(f"агент «{name}» не найден в МС")


def _org_id(ms_org):
    org = _ms("entity/organization", {"filter": f"name={ms_org}", "limit": 1})
    return _href_id(org["rows"][0]["meta"]["href"])


def posting_cogs_map(org_id):
    """{norm_posting: cogs} — FIFO-кэш отгрузок FBS+RFBS на их moment в МС."""
    rows = db.query("""SELECT demand_name, cogs FROM ms_demand_cogs
        WHERE org=%s AND agent IN ('Покупатель Озон','Озон Экспресс')""", (org_id,))
    out = defaultdict(float)
    for r in rows:
        out[norm_posting(r["demand_name"])] += float(r["cogs"] or 0)
    return out


def sku_unit_cost_global(account, org_id):
    """{sku: себест/шт} по ВСЕЙ истории аккаунта — кост-базис сторно возвратов. Считаем как
    импутацию в build(), но по всем месяцам сразу: matched-постинги (ms_demand_cogs) × их SKU из
    доставленных операций, делим себест поровну на SKU отправления, усредняем по штукам.
    Зачем глобально: возврат обычно приходит в ДРУГОМ месяце, чем продажа → месячный cpu_map
    вернувшегося SKU часто пуст; всеисторический cpu отражает фактически списанный себест/шт."""
    cogs_map = posting_cogs_map(org_id)   # norm_posting -> cogs (весь кэш)
    rows = db.query("""SELECT payload->'posting'->>'posting_number' post, payload->'items' items
        FROM raw_ozon_transaction WHERE account=%s
          AND payload->>'operation_type'='OperationAgentDeliveredToCustomer'""", (account,))
    cogs_acc, unit_acc = defaultdict(float), defaultdict(float)
    for r in rows:
        items = r["items"] or []
        skus = [str(i.get("sku")) for i in items if i.get("sku")]
        if not skus:
            continue
        c = cogs_map.get(norm_posting(r["post"]))
        if c is None:
            continue
        per = c / len(skus)
        for s in skus:
            cogs_acc[s] += per
            unit_acc[s] += 1
    return {s: cogs_acc[s] / unit_acc[s] for s in unit_acc if unit_acc[s] > 0}


def return_sellable_budget(org_id):
    """{norm_posting: Σ sellable ret_qty} из ms_return_cogs — бюджет штук, вернувшихся в
    ПРОДАВАЕМЫЙ сток (Звездный/Дисквер/Кантемировская, НЕ Брак/Озон). Кап сторно COGS: реверсим
    не больше, чем МС подтвердил как возврат в сток (корректно делит и смешанные постинги
    Брак/сток — сторнируем только сток-долю). Постинги без salesreturn → бюджет 0 → не сторним."""
    rows = db.query("""SELECT demand_name, ret_qty FROM ms_return_cogs
        WHERE org=%s AND sellable=true AND demand_name IS NOT NULL""", (org_id,))
    out = defaultdict(float)
    for r in rows:
        out[norm_posting(r["demand_name"])] += float(r["ret_qty"] or 0)
    return out


def _group_price_map():
    """{external_code: (min_nonzero, max_nonzero, is_set)} — групповая цена для fallback-COGS
    (как у ВБ): один external_code = группа аналогов с разной себестоимостью."""
    rows = db.query("""SELECT external_code,
        min(cost_seb) FILTER (WHERE cost_seb>0) mn,
        max(cost_seb) FILTER (WHERE cost_seb>0) mx,
        bool_or(title ILIKE '%%набор%%' OR title ILIKE '%%комплект%%') is_set
        FROM products WHERE external_code IS NOT NULL AND external_code<>'' GROUP BY external_code""")
    return {r["external_code"]: (r["mn"], r["mx"], r["is_set"]) for r in rows}


def _fallback_cpu(offer, gmap):
    """COGS/ед. по группе external_code: набор → max, одиночный → min (система берёт наименьший)."""
    g = gmap.get(offer)
    if not g:
        return None
    mn, mx, is_set = g
    if is_set and mx:
        return float(mx)
    return float(mn) if mn else None


def _rows(account, date_from, date_to):
    return [r["payload"] for r in db.query(
        """SELECT payload FROM raw_ozon_transaction
           WHERE account=%s AND (payload->>'operation_date')::date BETWEEN %s AND %s""",
        (account, date_from, date_to))]


def build(date_from="2026-06-01", date_to="2026-06-30", account="oz_acc1"):
    ms_org = ACC_ORG.get(account, ACC_ORG["oz_acc1"])
    ms_from = (datetime.date.fromisoformat(date_from) - datetime.timedelta(days=45)).isoformat()
    print(f"Ozon маржа {account} {date_from}..{date_to}", flush=True)
    org_id = _org_id(ms_org)
    cogs_map = posting_cogs_map(org_id)
    sell_budget = return_sellable_budget(org_id)   # norm_posting → штук в продаваемый сток (сторно)
    cpu_global = sku_unit_cost_global(account, org_id)  # sku → себест/шт (вся история) для сторно
    sku2offer = fetch_product_offer_map(account)   # sku → offer_id (=external_code)
    gmap = _group_price_map()

    ops = _rows(account, date_from, date_to)
    sku_fin = defaultdict(lambda: {c: 0.0 for c in CATEGORIES})
    sku_name = {}
    posting_skus = {}                 # norm_posting -> {"skus":[...], "schema":...}
    posting_rev = defaultdict(float)  # norm_posting -> выручка (для покрытия по деньгам)
    # Возврат порождает ДВЕ операции: денежную (ClientReturnAgentOperation, accr<0) и товарную
    # (OperationReturnGoodsFBSofRMS, accr≥0). Обе несут те же items[]. Считаем по каждому типу
    # отдельно → берём MAX на SKU (одна физическая единица, не сумма), затем кап по МС-бюджету.
    ret_money = defaultdict(lambda: defaultdict(float))  # norm_posting -> sku -> штук (денежная оп)
    ret_goods = defaultdict(lambda: defaultdict(float))  # norm_posting -> sku -> штук (товарная оп)
    overhead = {c: 0.0 for c in CATEGORIES}
    for op in ops:
        cats = categorize_operation(op)
        accr = float(op.get("accruals_for_sale") or 0)   # <0 = реверс доставленной продажи (возврат)
        skus = [str(i.get("sku")) for i in (op.get("items") or []) if i.get("sku")]
        if not skus:
            for c, v in cats.items():
                overhead[c] += v       # кампанийная реклама/подписка/эквайринг — оверхед
            continue
        share = 1.0 / len(skus)
        for it in op.get("items", []):
            if it.get("sku"):
                sku_name[str(it["sku"])] = it.get("name")
        for sku in skus:
            for c, v in cats.items():
                sku_fin[sku][c] += v * share
        post = (op.get("posting") or {}).get("posting_number")
        if post and cats["revenue"] > 0:
            key = norm_posting(post)
            posting_skus[key] = {
                "skus": skus, "schema": (op.get("posting") or {}).get("delivery_schema") or "—"}
            posting_rev[key] += cats["revenue"]
        elif post and (accr < 0 or op.get("operation_type") == "OperationReturnGoodsFBSofRMS"):
            # возврат: денежная оп (accr<0: ClientReturnAgentOperation/StornoDelivered) ИЛИ товарная
            # оп (OperationReturnGoodsFBSofRMS). items[] повторяется по штукам → qty = число записей.
            key = norm_posting(post)
            bucket = ret_money if accr < 0 else ret_goods
            for sku in skus:
                bucket[key][sku] += 1.0

    # COGS на SKU тремя слоями (как у ВБ) → покрытие ~100%:
    #   1) ТОЧНО: отправление сматчилось с МС-заказом → COGS из позиций (делим по ед.).
    #   2) ИМПУТАЦИЯ: непокрытое отправление, но у SKU есть COGS/ед. из его сматченных продаж.
    #   3) ГРУППА: fallback по offer_id=external_code (как WB-FBO), если импутации нет.
    # Единицы: items[] повторяется по штукам (qty 3 = 3 записи) → счёт по записям.
    sku_cogs = defaultdict(float)
    m_cogs, m_units = defaultdict(float), defaultdict(int)
    cov = {"matched": 0, "imputed": 0, "grouped": 0, "missed": 0}
    rev_cov = {"covered": 0.0, "missed": 0.0}
    miss_schema = defaultdict(int)
    miss_detail, unmatched = [], []
    for key, info in posting_skus.items():
        skus = info["skus"]
        rev = posting_rev.get(key, 0.0)
        c = cogs_map.get(key)
        if c is not None:
            per = c / len(skus)
            for s in skus:
                sku_cogs[s] += per
                m_cogs[s] += per
                m_units[s] += 1
            cov["matched"] += 1
            rev_cov["covered"] += rev
        else:
            unmatched.append((key, skus, info["schema"], rev))
    cpu_map = {s: m_cogs[s] / m_units[s] for s in m_units if m_units[s] > 0}
    for key, skus, schema, rev in unmatched:
        used_impute = used_group = 0
        for s in skus:
            if s in cpu_map:
                sku_cogs[s] += cpu_map[s]
                used_impute += 1
            else:
                fc = _fallback_cpu(sku2offer.get(s), gmap)
                if fc is not None:
                    sku_cogs[s] += fc
                    used_group += 1
        if used_impute + used_group == 0:
            cov["missed"] += 1
            rev_cov["missed"] += rev
            miss_schema[schema] += 1
            miss_detail.append((key, schema, rev, len(skus)))
        else:
            cov["imputed" if used_impute >= used_group else "grouped"] += 1
            rev_cov["covered"] += rev

    # --- сторно COGS возвратов в продаваемый сток (в месяце возврата) ---
    # Товар, вернувшийся в сток, при перепродаже спишет себест повторно → реверсим COGS исходной
    # продажи по себест/шт этого SKU (cpu_global — всеисторический, т.к. продажа была в другом
    # месяце; иначе группа по offer_id). Кап штук — по бюджету МС sellable на постинг (fail-closed
    # по постингам без salesreturn и по Брак/Озон; делит смешанные). used[k] — физически потрачено
    # штук по постингу: расходуем бюджет на КАЖДУЮ вернувшуюся штуку (даже без себеста), чтобы бюджет
    # не «утёк» другому SKU постинга. Допущение (проверено: 0 постингов в >1 месяце) — возврат по
    # постингу приходит в один месяц → бюджет не переиспользуется между месячными прогонами build().
    storno = defaultdict(float)
    used = defaultdict(float)
    for key in set(ret_money) | set(ret_goods):
        budget = sell_budget.get(key, 0.0)
        if budget <= 0:
            continue
        m, g = ret_money.get(key, {}), ret_goods.get(key, {})
        # штук на SKU = MAX(денежная, товарная) — одна физическая единица, не двойной счёт
        sku_q = {s: max(m.get(s, 0.0), g.get(s, 0.0)) for s in set(m) | set(g)}
        for s, q in sorted(sku_q.items(), key=lambda kv: -kv[1]):  # дорогие/крупные SKU первыми
            take = min(q, budget - used[key])
            if take <= 0:
                break
            used[key] += take
            cpu = cpu_global.get(s) or _fallback_cpu(sku2offer.get(s), gmap)
            if cpu:
                storno[s] += cpu * take

    # сборка строк + запись
    recs, rows_view = [], []
    for sku, f in sku_fin.items():
        rev = f["revenue"]
        cogs = sku_cogs.get(sku, 0.0) - storno.get(sku, 0.0)
        net = sum(f.values()) - cogs
        rec = {
            "article": sku, "platform": "ozon", "account": account,
            "period_from": date_from, "period_to": date_to, "qty": None,
            "revenue_buyer": rev, "cogs": cogs,
            "commission": -f["commission"], "logistics": -f["logistics"],
            "returns_sum": -f["returns"], "storage": -f["storage"],
            "acceptance": 0.0,
            "other": -(f["penalties"] + f["acquiring"] + f["advertising"]
                       + f["subscription"] + f["partners"] + f["points"]
                       + f["compensation"] + f["fbo"] + f["other"]),
            "net_profit": net, "margin_pct": (net / rev * 100) if rev else None,
            "commission_pct": (-f["commission"] / rev * 100) if rev else None,
        }
        recs.append(rec)
        rows_view.append((sku, sku_name.get(sku, ""), rev, cogs, net, rec["margin_pct"]))
    db.upsert("margin_by_sku", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to"])

    # --- сводка ---
    tot_payout = sum(sum(f.values()) for f in sku_fin.values()) + sum(overhead.values())
    tot_storno = sum(storno.values())
    tot_cogs = sum(sku_cogs.values()) - tot_storno   # net: списанный себест − сторно возвратов
    tot_rev = sum(f["revenue"] for f in sku_fin.values())
    total_p = sum(cov.values()) or 1
    covered = total_p - cov["missed"]
    rev_total = sum(rev_cov.values()) or 1
    print(f"\n  SKU с продажами: {sum(1 for f in sku_fin.values() if f['revenue']>0)}")
    print(f"  COGS-покрытие отправлений: {covered}/{total_p} ({covered/total_p*100:.0f}%) "
          f"= МС {cov['matched']} + импутация {cov['imputed']} + группа {cov['grouped']}; "
          f"без COGS {cov['missed']} {dict(miss_schema)}")
    print(f"  COGS-покрытие ПО ВЫРУЧКЕ: {rev_cov['covered']/rev_total*100:.1f}% "
          f"(без COGS {rev_cov['missed']:,.0f} ₽)".replace(",", " "))
    if miss_detail:
        print("  непокрытые (posting | schema | выручка | #ед.):")
        for key, sch, rev, ns in sorted(miss_detail, key=lambda x: -x[2])[:10]:
            print(f"    {key:<16} {sch:<5} {rev:>8,.0f}  ед={ns}".replace(",", " "))
    n_ret_post = sum(1 for k in (set(ret_money) | set(ret_goods)) if sell_budget.get(k, 0.0) > 0)
    print(f"  сторно возвратов в сток: {tot_storno:,.0f} ₽ по {len(storno)} SKU "
          f"({sum(used.values()):.0f} штук, {n_ret_post} постингов)".replace(",", " "))
    print(f"  записано в margin_by_sku: {len(recs)} SKU (platform=ozon)")

    print(f"\n=== ПОРТФЕЛЬ Ozon {account} {date_from}..{date_to} (₽) ===")
    for lbl, v in [("Выручка (SKU)", tot_rev),
                   ("− COGS брутто (из МС)", -(sum(sku_cogs.values()))),
                   ("+ Сторно COGS возвратов в сток", tot_storno),
                   ("Оверхед (реклама-кампании/подписка/эквайринг, вне SKU)",
                    sum(overhead.values()))]:
        print(f"  {lbl:<54}{v:>14,.0f}".replace(",", " "))
    print(f"  {'= ЧИСТАЯ ПРИБЫЛЬ (к перечислению − COGS)':<54}"
          f"{tot_payout - tot_cogs:>14,.0f}".replace(",", " "))

    print(f"\n=== ТОП-15 SKU по выручке ===")
    print(f"{'SKU':<12}{'выручка':>11}{'COGS':>11}{'чист.приб':>11}{'марж%':>7}  товар")
    for sku, name, rev, cogs, net, mp in sorted(rows_view, key=lambda x: -x[2])[:15]:
        mps = f"{mp:5.1f}" if mp is not None else "  —  "
        print(f"{sku:<12}{rev:>11,.0f}{cogs:>11,.0f}{net:>11,.0f}{mps:>7}  {(name or '')[:34]}"
              .replace(",", " "))

    print(f"\n=== 10 SKU-УБИЙЦ (отрицательная маржа, по выручке) ===")
    losers = [r for r in rows_view if r[4] < 0 and r[2] > 0]
    for sku, name, rev, cogs, net, mp in sorted(losers, key=lambda x: -x[2])[:10]:
        mps = f"{mp:5.1f}" if mp is not None else "  —  "
        print(f"{sku:<12}{rev:>11,.0f}{cogs:>11,.0f}{net:>11,.0f}{mps:>7}  {(name or '')[:34]}"
              .replace(",", " "))


if __name__ == "__main__":
    a = sys.argv
    build(a[1] if len(a) > 1 else "2026-06-01",
          a[2] if len(a) > 2 else "2026-06-30",
          a[3] if len(a) > 3 else "oz_acc1")

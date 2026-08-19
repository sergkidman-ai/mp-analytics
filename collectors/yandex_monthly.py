"""collectors/yandex_monthly.py — Яндекс.Маркет: история заказов + экономика по месяцам.

Бизнес-эндпоинт /orders отдаёт ~30 дней. Для ИСТОРИИ используем /campaigns/{id}/stats/orders
(принимает dateFrom/dateTo, отдаёт месяцы назад). В заказе — вся экономика:
payments (заплатил покупатель), subsidies (доплата Маркета), commissions[] по типам
(FEE=комиссия, DELIVERY_*=логистика, PAYMENT_TRANSFER=эквайринг, AUCTION_PROMOTION=буст-реклама,
AGENCY=агентское), статусы (RETURNED и т.п.), items.shopSku (наш артикул).

Пишем: сырьё → raw_yandex_stats_order; агрегаты → yandex_monthly (совместимость)
и yandex_finance_monthly (выручка/расходы/возвраты/COGS по месяцам).
Выручка = Σ payments без CANCELLED — сходится с учётной таблицей.

Запуск:
  ./venv/bin/python collectors/yandex_monthly.py [YYYY-MM-01 since]      # полный: тянет заказы из API
  ./venv/bin/python collectors/yandex_monthly.py --light [YYYY-MM-01]    # лёгкий: пересчёт из сырья, без API

Лёгкий режим (recompute) пересобирает витрину из уже собранного сырья
(raw_yandex_stats_order + raw_yandex_services + raw_yandex_closure + yandex_boost_monthly)
и НЕ обращается к stats/orders API. Применять, когда обновилось только сырьё (загружен отчёт
услуг / closure / себест) или когда полный пул «слишком большой, не проходит».
"""
import os
import sys
import time
import datetime
import pathlib
from collections import defaultdict, Counter

import requests
import psycopg2.extras
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors import yandex_closure  # noqa: E402
from reports import yandex_ad_contract  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"

RETURN_STATUSES = {"RETURNED", "PARTIALLY_RETURNED"}
COMM_COL = {"FEE": "fee", "PAYMENT_TRANSFER": "transfer",
            "AUCTION_PROMOTION": "promotion", "AGENCY": "agency"}


def _cfg():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    if not key or not camps:
        raise RuntimeError("YANDEX_API_KEY_ACC1 / YANDEX_CAMPAIGN_ID_ACC1 не заданы")
    return key, camps


def collect_offers():
    """Каталог offer-mappings (бизнес-уровень) → raw_yandex_offer: баркоды, закупочная, marketSku."""
    key = os.getenv("YANDEX_API_KEY_ACC1")
    biz = os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    buf, tok, n = [], None, 0
    for _ in range(200):
        params = {"limit": 200}
        if tok:
            params["page_token"] = tok
        r = requests.post(f"{API}/v2/businesses/{biz}/offer-mappings", headers=H, params=params,
                          json={}, timeout=90)
        r.raise_for_status()
        res = r.json().get("result", {})
        for om in res.get("offerMappings", []):
            o = om.get("offer") or {}
            if o.get("offerId"):
                buf.append({"account": ACCOUNT, "offer_id": str(o["offerId"]),
                            "payload": psycopg2.extras.Json(om)})
        if len(buf) >= 1000:
            n += db.upsert("raw_yandex_offer", buf, conflict_cols=["account", "offer_id"],
                           update_cols=["payload"])
            buf = []
        tok = (res.get("paging") or {}).get("nextPageToken")
        if not tok:
            break
        time.sleep(0.3)
    if buf:
        n += db.upsert("raw_yandex_offer", buf, conflict_cols=["account", "offer_id"],
                       update_cols=["payload"])
    print(f"  каталог офферов: {n} записано", flush=True)
    return n


def _report_csv(path, body, timeout=180):
    """Асинхронный отчёт Партнёр-API → список dict-строк CSV (архив может содержать несколько CSV —
    возвращаем по имени файла)."""
    import io
    import csv
    import zipfile
    key = os.getenv("YANDEX_API_KEY_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    r = requests.post(f"{API}{path}", headers=H, params={"format": "CSV"}, json=body, timeout=60)
    r.raise_for_status()
    rid = r.json()["result"]["reportId"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        i = requests.get(f"{API}/reports/info/{rid}", headers=H, timeout=30).json().get("result", {})
        if i.get("status") == "DONE":
            f = requests.get(i["file"], timeout=120)
            out = {}
            with zipfile.ZipFile(io.BytesIO(f.content)) as z:
                for name in z.namelist():
                    if name.endswith(".csv"):
                        out[name] = list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8")))
            return out
        if i.get("status") == "FAILED":
            raise RuntimeError(f"отчёт {path} FAILED: {i.get('subStatus')}")
        time.sleep(3)
    raise RuntimeError(f"отчёт {path}: таймаут {timeout}с")


def collect_boost(months):
    """Реклама Маркета по месяцам из отчётов продвижения → yandex_boost_monthly.
    months — список 'YYYY-MM-01'. Буст продаж — отчёт на месяц (строки без дат);
    буст показов — один отчёт на весь диапазон (REAL_COST по дням)."""
    import calendar
    biz = os.getenv("YANDEX_BUSINESS_ID_ACC1")
    today = datetime.date.today().isoformat()
    res = {mo: {"sales_boost": 0.0, "shows_boost": 0.0} for mo in months}
    for mo in months:
        y, m = int(mo[:4]), int(mo[5:7])
        # dateTo в будущем API не принимает (400) — обрезаем текущий месяц по сегодня
        d_to = min(f"{mo[:7]}-{calendar.monthrange(y, m)[1]:02d}", today)
        try:
            csvs = _report_csv("/reports/boost-consolidated/generate",
                               {"businessId": int(biz), "dateFrom": mo, "dateTo": d_to})
            rows = next((v for k, v in csvs.items() if "boost" in k), [])
            res[mo]["sales_boost"] = round(sum(float(r.get("BILLED_AMOUNT") or 0) for r in rows), 2)
        except Exception as e:
            print(f"  [ya boost] продажи {mo[:7]}: {e}", flush=True)
            res[mo]["sales_boost"] = None
        time.sleep(1)
    try:
        y, m = int(months[-1][:4]), int(months[-1][5:7])
        d_to = min(f"{months[-1][:7]}-{calendar.monthrange(y, m)[1]:02d}", today)
        csvs = _report_csv("/reports/shows-boost/generate",
                           {"businessId": int(biz), "dateFrom": months[0], "dateTo": d_to,
                            "attributionType": "CLICKS"})
        rows = next((v for k, v in csvs.items() if "campaigns" in k), [])
        by_mo = defaultdict(float)
        for r in rows:
            d = (r.get("DATE") or "")[:7]
            by_mo[d + "-01"] += float(r.get("REAL_COST") or 0)
        for mo in months:
            res[mo]["shows_boost"] = round(by_mo.get(mo, 0.0), 2)
    except Exception as e:
        print(f"  [ya boost] показы: {e}", flush=True)
        for mo in months:
            res[mo]["shows_boost"] = None
    recs = [{"account": ACCOUNT, "month": mo, **v} for mo, v in res.items()
            if v["sales_boost"] is not None or v["shows_boost"] is not None]
    if recs:
        db.upsert("yandex_boost_monthly", recs, conflict_cols=["account", "month"],
                  update_cols=["sales_boost", "shows_boost"])
    for r in recs:
        print(f"  буст {r['month'][:7]}: продажи {r['sales_boost'] or 0:,.0f} + "
              f"показы {r['shows_boost'] or 0:,.0f}", flush=True)
    return res


def _ms_cogs_monthly(since="2026-01-01"):
    """ФАКТ себеста Маркета по месяцам — FIFO конкретных отгрузок из раздела «Себестоимость»
    (`ya_cogs_demand`, метод `ms_fifo` = `byoperation` по документу отгрузки МС).

    Решение Сергея 2026-08-13: единственный источник себестоимости отгрузки — FIFO. Прежний
    расчёт (Σ `products.cost_seb` × qty по позициям МС-ЗАКАЗА) брал среднюю себестоимость
    текущего остатка из карточки, а не стоимость конкретного списания, — заменён. Дельта
    янв–авг 2026: −210 182 ₽ COGS (то есть чистая на столько выше).

    В расход идёт только реализованное покупателю (`status='done'`). Возврат в сток, возврат
    браком и невыкуп (`CANCELLED_IN_DELIVERY`) выручки в отчёте не имеют → их себестоимость
    не расход отчёта МП; потери от брака живут в разделе «Себестоимость», не здесь.

    Месяц — по дате ЗАКАЗА (`creationDate` из stats/orders, мост `demand_name` = `id` заказа),
    потому что по ней же нарезана выручка в `_fold_order`: себест обязан лежать в том же месяце,
    что и выручка своей продажи, иначе строки отчёта несопоставимы. Раздел «Себестоимость»
    нарезан по месяцу ОТГРУЗКИ — отсюда расхождение помесячных итогов двух разделов на
    заказах, перешедших через границу месяца (в сумме за период сходится). Заказ, которого
    нет в сырье отчёта, кладём в месяц отгрузки.

    Заказ, которого нет в сырье отчёта, себеста НЕ лишается: `ya_cogs_demand._classify`
    относит его к `done` (дыра в сырье stats/orders, а не факт отсутствия продажи).

    Источник — таблица, а не МойСклад: сеть здесь не нужна, но `collectors.ya_cogs_demand`
    должен отработать РАНЬШЕ (в `run_daily.py` шаг стоит перед помесячной сборкой).
    """
    rows = db.query("""
        WITH ord AS (   -- месяц ЗАКАЗА из сырья отчёта: мост demand_name = stats/orders.id
            SELECT DISTINCT ON (payload->>'id') payload->>'id' oid,
                   substr(payload->>'creationDate', 1, 7) cm
            FROM raw_yandex_stats_order WHERE account = %s
            ORDER BY payload->>'id', loaded_at DESC),
        d AS (
            SELECT coalesce(o.cm, to_char(c.ym, 'YYYY-MM')) || '-01' mo,
                   c.cogs, c.status, c.method
            FROM ya_cogs_demand c LEFT JOIN ord o ON o.oid = c.demand_name
            WHERE c.account = %s)
        SELECT mo,
               sum(cogs) FILTER (WHERE status = 'done')                    fact,
               count(*)  FILTER (WHERE status = 'done')                    n_done,
               count(*)  FILTER (WHERE status <> 'done')                   n_norev,
               sum(cogs) FILTER (WHERE status <> 'done')                   sum_norev,
               count(*)  FILTER (WHERE status = 'done' AND method <> 'ms_fifo') n_nofifo
        FROM d WHERE mo >= %s
        GROUP BY 1 ORDER BY 1""", (ACCOUNT, ACCOUNT, since[:7] + "-01"))
    out, n_norev, sum_norev, n_nofifo = {}, 0, 0.0, 0
    for r in rows:
        out[r["mo"]] = float(r["fact"] or 0)
        n_norev += r["n_norev"]
        sum_norev += float(r["sum_norev"] or 0)
        n_nofifo += r["n_nofifo"]
    if not rows:
        print("  [ya monthly] ya_cogs_demand пуст — себест по фолбэк-карте SKU "
              "(запусти collectors.ya_cogs_demand)", flush=True)
        return {}
    print(f"  [ya monthly] себест = FIFO отгрузок: {len(rows)} мес, "
          f"отброшено {n_norev} отгрузок без начисленной выручки (отмена/возврат/брак) "
          f"на {sum_norev:,.0f} ₽".replace(",", " "), flush=True)
    if n_nofifo:
        print(f"  [ya monthly] ВНИМАНИЕ: {n_nofifo} учтённых отгрузок без FIFO "
              f"(импутация/ручной) — проверь раздел «Себестоимость»", flush=True)
    return out


def _cost_map():
    """Себест по offerId (=shopSku), цепочка: yandex_cost (факт МС-заказов Маркета) →
    products.cost_seb по external_code → баркод оффера→ms_barcode→МС (cost_seb, потом buy_price) →
    закупочная из карточки ЯМ (purchasePrice). Возвращает {sku: (cost, источник)}."""
    ext = {r["external_code"]: float(r["c"]) for r in db.query(
        """SELECT external_code, min(cost_seb) c FROM products
           WHERE external_code IS NOT NULL AND cost_seb>0 GROUP BY 1""")}
    yc = {r["offer"]: float(r["cost_per_unit"]) for r in db.query(
        "SELECT offer, cost_per_unit FROM yandex_cost WHERE offer NOT LIKE '\\_\\_%%' AND cost_per_unit>0")}
    bc2ms = {r["barcode"]: r["ms_id"] for r in db.query("SELECT barcode, ms_id FROM ms_barcode")}
    seb_ms = {r["ms_id"]: float(r["cost_seb"]) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}
    buy_ms = {r["ms_id"]: float(r["buy_price"]) for r in db.query(
        "SELECT ms_id, buy_price FROM ms_product WHERE buy_price>0")}
    out = {}
    for sku, c in ext.items():
        out[sku] = (c, "ext")
    for sku, c in yc.items():
        out[sku] = (c, "yc")
    # Тянем из payload ТОЛЬКО два нужных поля, а не весь JSONB: db.query делает fetchall(), и
    # полный payload 15k офферов (33 МБ на диске) разворачивался в ~330 МБ Python-объектов —
    # на этом пике run_daily ловил OOM (04.08.2026). Узкий SELECT: пик 364 → 40 МБ, карта SKU
    # побайтово та же.
    for r in db.query("""SELECT offer_id, payload->'offer'->'barcodes' AS barcodes,
                                payload->'offer'->'purchasePrice'->>'value' AS pp
                         FROM raw_yandex_offer WHERE account=%s""", (ACCOUNT,)):
        sku = r["offer_id"]
        if sku in out:
            continue
        msids = [bc2ms[b] for b in (r["barcodes"] or []) if b in bc2ms]
        cs = [seb_ms[m] for m in msids if m in seb_ms]
        bs = [buy_ms[m] for m in msids if m in buy_ms]
        pp = float(r["pp"] or 0)
        if cs:
            out[sku] = (min(cs), "bc")
        elif bs:
            out[sku] = (min(bs), "bc")
        elif pp > 0:
            out[sku] = (pp, "pp")
    return out


def _comm_col(ctype):
    if ctype in COMM_COL:
        return COMM_COL[ctype]
    if "DELIVERY" in (ctype or ""):
        return "delivery"
    return "other_fee"


def _pay_sum(o):
    """Деньги покупателя по заказу: PAYMENT − REFUND (у возврата REFUND идёт с плюсом!)."""
    pay = refund = 0.0
    for p in (o.get("payments") or []):
        if p.get("type") == "REFUND":
            refund += p.get("total", 0) or 0
        else:
            pay += p.get("total", 0) or 0
    return pay - refund, refund


def _fold_order(o, fin, agg, comm_types, cost):
    """Свернуть один заказ stats/orders в помесячные агрегаты fin/agg (мутирует их).
    Единая логика для live-пула (collect) и пересчёта из сырья (recompute) — источник
    заказа (API или raw_yandex_stats_order) значения не имеет, payload идентичен."""
    cd = (o.get("creationDate") or "")[:7]   # YYYY-MM
    if len(cd) != 7:
        return
    mo = cd + "-01"
    st = o.get("status") or ""
    f = fin[mo]
    # Счётчик «Возвраты, шт» = ФИЗИЧЕСКИЙ возврат товара на склад: товарные возвраты (RETURNED)
    # + невыкупы (CANCELLED_IN_DELIVERY — покупатель не забрал, товар вернулся на сток). Считаем по
    # МЕСЯЦУ ВОЗВРАТА (statusUpdateDate = когда заказ стал возвратом/невыкупом), а НЕ по месяцу заказа —
    # сверено с МС salesreturn (июнь: RETURNED 17 + невыкуп 27 = 44 ≈ МС 39). Невыкуп остаётся ещё и
    # отдельной метрикой убытка логистики (unredeemed_*, по месяцу заказа, ниже). Деньги возвратов
    # (returns_sum) идут из closure ЛК строкой «Возвращено потребителям» — шире (все возвраты покупателям,
    # вкл. невыкупы и отмены), поэтому счётчик и деньги намеренно в разных осях: склад vs касса.
    if st in RETURN_STATUSES or st == "CANCELLED_IN_DELIVERY":
        sud = (o.get("statusUpdateDate") or "")[:7]
        rmo = (sud + "-01") if len(sud) == 7 else mo
        fin[rmo]["returns_orders"] += 1
    # ОТМЕНЫ: статусы CANCELLED_* (ровно 'CANCELLED' не бывает!). Деньги покупателя
    # самокорректны (PAYMENT−REFUND=0), но субсидию и счётчик заказов НЕ включаем.
    # Комиссии отменённых (логистика незабора) — реальный расход, учитываем;
    # незабор = CANCELLED_IN_DELIVERY — отдельной метрикой.
    if st.startswith("CANCELLED"):
        o_comm = 0.0
        for c in (o.get("commissions") or []):
            comm_types[c.get("type")] += 1
            v = c.get("actual", 0) or 0
            f[_comm_col(c.get("type"))] += v
            o_comm += v
        if st == "CANCELLED_IN_DELIVERY":
            f["unredeemed_orders"] += 1
            f["unredeemed_cost"] += o_comm
        return
    a = agg[mo]
    a["orders"] += 1
    pay, refund = _pay_sum(o)
    sub = sum(s.get("amount", 0) or 0 for s in (o.get("subsidies") or []))
    a["revenue"] += pay
    a["subsidy"] += sub
    f["revenue"] += pay
    f["subsidy"] += sub
    f["orders"] += 1
    f["returns_sum"] += refund
    for c in (o.get("commissions") or []):
        comm_types[c.get("type")] += 1
        f[_comm_col(c.get("type"))] += c.get("actual", 0) or 0
    # COGS: без отмен и возвратов (товар вернулся — себест не списываем)
    if st not in RETURN_STATUSES:
        for it in (o.get("items") or []):
            q = it.get("count", 0) or 0
            sku = str(it.get("shopSku") or "")
            f["qty"] += q
            if sku in cost:
                f["cogs"] += cost[sku] * q
                f["qty_cov"] += q


def _write_finance(fin, agg, since):
    """Аггрегатная фаза: fin/agg → yandex_monthly + yandex_finance_monthly.
    Общая для collect (live) и recompute (из сырья). Расходы/реклама — из
    raw_yandex_services; выручка/возвраты — из raw_yandex_closure (фолбэк на stats/orders).
    Возвращает список записанных помесячных строк (frecs)."""
    recs = [{"account": ACCOUNT, "month": mo, "revenue": round(v["revenue"], 2),
             "subsidy": round(v["subsidy"], 2), "orders": v["orders"]}
            for mo, v in sorted(agg.items())]
    if recs:
        db.upsert("yandex_monthly", recs, conflict_cols=["account", "month"],
                  update_cols=["revenue", "subsidy", "orders"])
    # COGS: приоритет — ФАКТ из МС-заказов Маркета за месяц; фолбэк — карта по SKU с импутацией
    ms_fact = {}
    try:
        ms_fact = _ms_cogs_monthly(since)
    except Exception as e:  # МС недоступен — работаем по карте
        print(f"  [ya monthly] МС-факт себеста недоступен: {e}", flush=True)
    # Реклама, приоритет источника:
    #   1) отчёт о стоимости услуг из ЛК (raw_yandex_services) — покрывает ВСЕ месяцы,
    #      включая янв–апр, куда API продвижения не отдаёт (буст продаж/показов+Полки+баннеры);
    #   2) API-отчёты продвижения (yandex_boost_monthly) — фолбэк на май–июнь;
    #   3) AUCTION_PROMOTION из заказов (f["promotion"]) — грубый фолбэк (~5%).
    # Май–июнь по бусту эти источники совпали до рубля (сверено).
    boost = {r["month"].isoformat(): float(r["sales_boost"] or 0) + float(r["shows_boost"] or 0)
             for r in db.query(
                 "SELECT month, sales_boost, shows_boost FROM yandex_boost_monthly WHERE account=%s",
                 (ACCOUNT,))}
    # Расходные категории (комиссия/логистика/эквайринг/прочее) и реклама — из ОФИЦИАЛЬНОГО
    # «Отчёта о стоимости услуг» (raw_yandex_services), сверено с ЛК до копейки. Реклама — раздельно.
    svc = {r["ym"]: r for r in db.query("""
        SELECT ym,
               sum(cost) FILTER (WHERE category='commission')::float   commission,
               sum(cost) FILTER (WHERE category='logistics')::float    logistics,
               sum(cost) FILTER (WHERE category='acquiring')::float    acquiring,
               sum(cost) FILTER (WHERE category='misc')::float         misc,
               sum(cost) FILTER (WHERE category='boost_sales')::float  boost_sales,
               sum(cost) FILTER (WHERE category='boost_shows')::float  boost_shows,
               sum(cost) FILTER (WHERE category='shelf')::float        shelf,
               sum(cost) FILTER (WHERE category IN ('boost_sales','boost_shows','shelf'))::float ad,
               sum(cost) FILTER (WHERE category='subscription')::float subscription,
               sum(cost) FILTER (WHERE category='reviews')::float      reviews,
               -- зачёт баллами Маркета: услуга начислена, но погашена не деньгами, а субсидией.
               -- Нужен, чтобы показать оборот и удержания ГРОСС (как «Баллы» у Ozon и СПП у ВБ):
               -- начислено = cost + netting. На деньги и на «Итого к перечислению» не влияет.
               sum(coalesce(netting,0))::float                          netting,
               sum(coalesce(netting,0)) FILTER (WHERE category='commission')::float netting_fee,
               sum(coalesce(netting,0)) FILTER (WHERE category='logistics')::float  netting_delivery,
               sum(coalesce(netting,0)) FILTER (
                   WHERE category IN ('boost_sales','boost_shows','shelf','reviews'))::float netting_promotion,
               sum(coalesce(netting,0)) FILTER (WHERE category='boost_sales')::float netting_boost_sales,
               sum(coalesce(netting,0)) FILTER (WHERE category='boost_shows')::float netting_boost_shows,
               sum(coalesce(netting,0)) FILTER (WHERE category='shelf')::float       netting_shelf
        FROM raw_yandex_services WHERE account=%s GROUP BY ym""", (ACCOUNT,))}
    # Выручка и возвраты — из детализированного отчёта о схождении с закрывающими
    # документами (raw_yandex_closure), сверено с ЛК до копейки. Фолбэк на stats/orders,
    # если closure за месяц не собран. См. collectors/yandex_closure.py.
    clo = yandex_closure.closure_monthly(ACCOUNT)
    # Реклама по отдельному договору на размещение (354817/19) — ручной ввод, Партнёр-API
    # её не отдаёт ни одним отчётом (проверено 17.08.2026). См. reports/yandex_ad_contract.py.
    ad_contract = yandex_ad_contract.monthly(ACCOUNT)
    if ad_contract:
        print(f"  [ya monthly] реклама по договорам: {len(ad_contract)} мес, "
              f"{sum(ad_contract.values()):,.2f} ₽".replace(",", " "), flush=True)
    frecs = []
    for mo, f in sorted(fin.items()):
        map_cogs = round(f["cogs"] + (f["qty"] - f["qty_cov"]) * (f["cogs"] / f["qty_cov"])
                         if f["qty_cov"] else f["cogs"], 2)
        fact = ms_fact.get(mo)
        s = svc.get(mo[:7]) or {}
        # Выручка/возвраты — из closure-отчёта (истина ЛК). Гросс-поступления от покупателей
        # (не нетто по заказу) и возвраты как отдельная строка; фолбэк — значения stats/orders.
        # Признак полноты месяца — наличие распознанной выручки (payments): коллектор пишет
        # только полные снапшоты, а пустой/битый месяц (revenue=0) НЕ должен занулять stats.
        c = clo.get(mo[:7])
        if c and c["revenue"]:
            f["revenue"] = c["revenue"]
            f["returns_sum"] = c["returns"]     # положительный модуль (как в stats-конвенции)
        # Расходные — из отчёта услуг (истина), НЕ из stats/orders commissions[] (иначе задвоение).
        # Если отчёт за месяц не собран — остаётся значение из stats/orders (фолбэк).
        if s.get("commission") is not None:
            f["fee"] = s["commission"]
        if s.get("logistics") is not None:
            f["delivery"] = s["logistics"]
        if s.get("acquiring") is not None:
            f["transfer"] = s["acquiring"]
        if s.get("misc") is not None:
            f["other_fee"] = s["misc"]
        # реклама раздельно (Fix 3). «Программа лояльности и отзывы» (категория reviews) —
        # это тоже продвижение: включаем её в promotion как компонент (наравне с бустами/полками),
        # а reviews_cost остаётся справочной под-строкой (НЕ отдельным удержанием, иначе двойной счёт).
        f["boost_sales"] = s.get("boost_sales") or 0.0
        f["boost_shows"] = s.get("boost_shows") or 0.0
        f["shelf"] = s.get("shelf") or 0.0
        loyalty = s.get("reviews") or 0.0        # Программа лояльности и отзывы → в продвижение
        # Реклама по отдельному договору на размещение (354817/19): в Партнёр-API её нет
        # ни в одном отчёте, суммы вводятся руками (reports/yandex_ad_contract.py).
        # Считаем её компонентом продвижения — как бусты, полки и лояльность.
        adc = ad_contract.get(mo, 0.0)
        if s.get("ad") is not None:      # отчёт услуг за месяц собран (даже если реклама = 0)
            f["promotion"] = s["ad"] + loyalty + adc
        elif mo in boost:                # иначе фолбэк на API продвижения
            f["promotion"] = boost[mo] + loyalty + adc
        else:                            # услуг нет вовсе — договор всё равно расход
            f["promotion"] = f["promotion"] + adc
        frecs.append({"account": ACCOUNT, "month": mo,
                      "revenue": round(f["revenue"], 2), "subsidy": round(f["subsidy"], 2),
                      "orders": int(f["orders"]),
                      "returns_orders": int(f["returns_orders"]), "returns_sum": round(f["returns_sum"], 2),
                      "fee": round(f["fee"], 2), "delivery": round(f["delivery"], 2),
                      "transfer": round(f["transfer"], 2), "promotion": round(f["promotion"], 2),
                      "agency": round(f["agency"], 2), "other_fee": round(f["other_fee"], 2),
                      "subscription_cost": round(s.get("subscription") or 0, 2),
                      "reviews_cost": round(s.get("reviews") or 0, 2),
                      "boost_sales": round(f["boost_sales"], 2),
                      "boost_shows": round(f["boost_shows"], 2),
                      "shelf": round(f["shelf"], 2),
                      "ad_contract": round(adc, 2),
                      "netting": round(s.get("netting") or 0, 2),
                      "netting_fee": round(s.get("netting_fee") or 0, 2),
                      "netting_delivery": round(s.get("netting_delivery") or 0, 2),
                      "netting_promotion": round(s.get("netting_promotion") or 0, 2),
                      "netting_boost_sales": round(s.get("netting_boost_sales") or 0, 2),
                      "netting_boost_shows": round(s.get("netting_boost_shows") or 0, 2),
                      "netting_shelf": round(s.get("netting_shelf") or 0, 2),
                      "unredeemed_orders": int(f["unredeemed_orders"]),
                      "unredeemed_cost": round(f["unredeemed_cost"], 2),
                      # fact is not None (а не truthy): нетто-COGS после сторно возвратов может
                      # оказаться ровно 0 за месяц — это валидный факт, не повод падать на map_cogs.
                      "cogs": round(fact, 2) if fact is not None else map_cogs,
                      "cogs_cov_pct": 100.0 if fact is not None else (
                          round(f["qty_cov"] / f["qty"] * 100, 1) if f["qty"] else 0)})
    if frecs:
        db.upsert("yandex_finance_monthly", frecs, conflict_cols=["account", "month"],
                  update_cols=["revenue", "subsidy", "orders", "returns_orders", "returns_sum",
                               "fee", "delivery", "transfer", "promotion", "agency", "other_fee",
                               "subscription_cost", "reviews_cost",
                               "boost_sales", "boost_shows", "shelf", "ad_contract",
                               "netting", "netting_fee", "netting_delivery", "netting_promotion",
                               "netting_boost_sales", "netting_boost_shows", "netting_shelf",
                               "unredeemed_orders", "unredeemed_cost", "cogs", "cogs_cov_pct"])
        db.execute("UPDATE yandex_finance_monthly SET updated_at=now() WHERE account=%s", (ACCOUNT,))
    for r in frecs:
        mp = (r["fee"] + r["delivery"] + r["transfer"] + r["promotion"] + r["agency"]
              + r["other_fee"] + r["subscription_cost"])  # reviews_cost уже внутри promotion
        print(f"  {r['month'][:7]}: выручка {r['revenue']:,.0f} | субсидия {r['subsidy']:,.0f} | "
              f"заказов {r['orders']} | возвратов {r['returns_orders']} ({r['returns_sum']:,.0f}) | "
              f"незаборов {r['unredeemed_orders']} ({r['unredeemed_cost']:,.0f}) | "
              f"расходы МП {mp:,.0f} (комиссия {r['fee']:,.0f}, логистика {r['delivery']:,.0f}, "
              f"эквайринг {r['transfer']:,.0f}, продвижение {r['promotion']:,.0f} "
              f"(вкл. лояльность {r['reviews_cost']:,.0f}), подписка {r['subscription_cost']:,.0f}) | "
              f"COGS {r['cogs']:,.0f} ({r['cogs_cov_pct']:.0f}%)", flush=True)
    return frecs


def recompute(since="2026-01-01"):
    """ЛЁГКИЙ режим: пересобрать витрину из УЖЕ собранного сырья, БЕЗ обращения к API Яндекса.
    Заказы читаются из raw_yandex_stats_order (payload идентичен ответу API), расходы/реклама —
    из raw_yandex_services, выручка/возвраты — из raw_yandex_closure, буст — из yandex_boost_monthly.
    Выхлоп идентичен collect(), но без тяжёлого stats/orders-пула (×3 кампании). Применять,
    когда обновилось только сырьё (загружен отчёт услуг / closure / себест), а перетягивать
    заказы не нужно — или когда полный пул «слишком большой, не проходит»."""
    cmap = _cost_map()
    cost = {sku: c for sku, (c, _src) in cmap.items()}
    print(f"  карта себеста: {len(cost)} SKU", flush=True)
    fin = defaultdict(lambda: defaultdict(float))
    agg = defaultdict(lambda: {"revenue": 0.0, "subsidy": 0.0, "orders": 0})
    comm_types = Counter()
    # Ограничиваем набор заказов ровно как live-пул: только КАМПАНИИ из конфига и окно с даты
    # since (в API это dateFrom, точность до дня). Без этого пересчёт учёл бы «осевшие» в сырье
    # заказы снятых из конфига кампаний и отличался бы от collect() на этих данных.
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    q = "SELECT payload FROM raw_yandex_stats_order WHERE account=%s"
    params = [ACCOUNT]
    if camps:
        q += " AND campaign_id = ANY(%s)"
        params.append(camps)
    n = 0
    for row in db.query(q, tuple(params)):
        o = row["payload"] or {}
        if ((o.get("creationDate") or "")[:10]) < since:   # окно как у live-пула (dateFrom=since, до дня)
            continue
        _fold_order(o, fin, agg, comm_types, cost)
        n += 1
    print(f"  пересчёт из сырья: {n} заказов (с {since})", flush=True)
    frecs = _write_finance(fin, agg, since)
    print(f"Яндекс.Маркет (лёгкий пересчёт): помесячно {len(frecs)} месяцев | "
          f"типы commissions: {dict(comm_types)}", flush=True)
    return frecs


def collect(since="2026-01-01"):
    key, camps = _cfg()
    H = {"Api-Key": key, "Content-Type": "application/json"}
    today = datetime.date.today().isoformat()
    # Себест: цепочка yandex_cost → external_code → баркод → закупочная ЯМ (см. _cost_map)
    cmap = _cost_map()
    cost = {sku: c for sku, (c, _src) in cmap.items()}
    src_cnt = Counter(src for _, src in cmap.values())
    print(f"  карта себеста: {len(cost)} SKU ({dict(src_cnt)})", flush=True)
    agg = defaultdict(lambda: {"revenue": 0.0, "subsidy": 0.0, "orders": 0})
    fin = defaultdict(lambda: defaultdict(float))
    comm_types = Counter()
    raw_buf, n_raw = [], 0
    for cid in camps:
        tok = None
        for _ in range(200):
            params = {"page_token": tok} if tok else {}
            r = requests.post(f"{API}/campaigns/{cid}/stats/orders", headers=H, params=params,
                              json={"dateFrom": since, "dateTo": today}, timeout=120)
            if r.status_code != 200:
                print(f"  [ya monthly] cid {cid}: HTTP {r.status_code} — стоп", flush=True)
                break
            res = r.json().get("result", {})
            for o in res.get("orders", []):
                oid = o.get("id")
                if oid is not None:
                    raw_buf.append({"account": ACCOUNT, "order_id": str(oid),
                                    "campaign_id": str(cid),
                                    "payload": psycopg2.extras.Json(o)})
                _fold_order(o, fin, agg, comm_types, cost)
            if len(raw_buf) >= 500:
                n_raw += db.upsert("raw_yandex_stats_order", raw_buf,
                                   conflict_cols=["account", "order_id"],
                                   update_cols=["campaign_id", "payload"])
                raw_buf = []
            tok = (res.get("paging") or {}).get("nextPageToken")
            if not tok:
                break
            time.sleep(0.5)
    if raw_buf:
        n_raw += db.upsert("raw_yandex_stats_order", raw_buf,
                           conflict_cols=["account", "order_id"],
                           update_cols=["campaign_id", "payload"])
    frecs = _write_finance(fin, agg, since)
    print(f"Яндекс.Маркет: сырья {n_raw} заказов, помесячно {len(frecs)} месяцев | "
          f"типы commissions: {dict(comm_types)}", flush=True)


def main():
    # Лёгкий режим: yandex_monthly.py --light [since] — пересчёт витрины из уже собранного
    # сырья без API-пула (когда обновилось только сырьё или полный пул «не проходит»).
    argv = sys.argv[1:]
    light = any(a in ("--light", "--recompute", "recompute") for a in argv)
    pos = [a for a in argv if not a.startswith("-") and a not in ("recompute",)]
    since = pos[0] if pos else "2026-01-01"
    if light:
        print(f"Яндекс.Маркет: ЛЁГКИЙ пересчёт витрины из сырья с {since} (без API)", flush=True)
        recompute(since)
        return
    print(f"Яндекс.Маркет помесячно с {since} (stats/orders)", flush=True)
    collect_offers()
    # буст: ежедневно освежаем текущий+прошлый месяц (закрытые месяцы не меняются)
    today = datetime.date.today()
    cur = today.replace(day=1)
    prev = (cur - datetime.timedelta(days=1)).replace(day=1)
    collect_boost([prev.isoformat(), cur.isoformat()])
    collect(since)


if __name__ == "__main__":
    main()

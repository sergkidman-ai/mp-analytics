# поток: mkt — добрать поисковые запросы Джема по МЁРТВЫМ рекламным SKU.
#
# Зачем: коллектор wb_jam берёт товары по `ORDER BY open_card DESC` — у мёртвых открытий ноль,
# поэтому они НИКОГДА не попадали в запрос, и покрытие Джемом по ним было 118 из 5817 (2%).
# Вопрос «что за запросы приносят показы мёртвым» без этих данных не отвечается: пословной
# статистики РЕКЛАМЫ у ВБ нет вовсе (проверен 21 путь /adv/* и /api/advert/v1/* — все 404).
#
# Мёртвый = крутится в рекламе (есть в wb_ad_nm_daily), но ноль открытий карточки в воронке
# за текущий месяц. Берём топ по рекламным показам — именно они съедают показы.
#
#   ./venv/bin/python -m ops.wb_jam_dead_texts --limit 400
import sys, argparse
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from collectors import wb_jam

ap = argparse.ArgumentParser()
ap.add_argument("--account", default="wb_acc1")
ap.add_argument("--limit", type=int, default=400, help="сколько мёртвых SKU опросить")
ap.add_argument("--days", type=int, default=7, help="окно Джема, дней")
A = ap.parse_args()

nms = [r["nm_id"] for r in db.query("""
    select d.nm_id, sum(d.views) v
      from wb_ad_nm_daily d
      left join wb_funnel f on f.nm_id = d.nm_id and f.account = d.account
           and f.period = date_trunc('month', current_date)::date
     where d.account = %s
     group by d.nm_id
    having coalesce(sum(f.open_count), 0) = 0 and sum(d.views) > 0
     order by 2 desc
     limit %s
""", (A.account, A.limit))]

cur, past = wb_jam._periods(A.days)
print(f"мёртвых к опросу: {len(nms)}, окно {cur['start']}…{cur['end']}, пауза {wb_jam.PAUSE}с "
      f"(~{len(nms) * wb_jam.PAUSE // 60} мин)", flush=True)
wb_jam.fetch_search_texts(A.account, nms, cur, past)

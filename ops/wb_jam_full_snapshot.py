# поток: mkt — ПОЛНЫЙ слепок пословной статистики Джема по всему каталогу wb_acc1.
#
# Зачем. За Джем платим 2.5% с оборота — в среднем 77 тыс. ₽/мес, 13.5% чистой прибыли
# (замер 12.08.2026: с февраля по август 540 564 ₽ при обороте 21.6 млн). При этом
# wb_search_text покрывал 1280 товаров из 10 668 — двенадцать процентов каталога, потому что
# коллектор wb_jam отбирает товары `ORDER BY open_card DESC`. Мы платили за всё, а выкачивали
# восьмую часть.
#
# Что делает. Пока подписка оплачена, снимает карту «запрос → товар → частотность» по ВСЕМУ
# каталогу. Слепок остаётся в нашей БД навсегда и переживёт отключение подписки: частотность
# модельных кодов картриджей меняется медленно. Новых трат не создаёт — ходит по уже
# оплаченному ключу.
#
# Возобновляемость. ~10.7 тыс. товаров × 5 с ≈ 15 часов; рвать такой прогон нельзя без потерь,
# поэтому обработанные nm пишутся в файл-прогресс и при повторном запуске пропускаются.
# Товары, по которым Джем вернул пусто, тоже помечаются обработанными — иначе каждый перезапуск
# будет долбиться в них снова (а таких большинство: у мёртвых запросов нет вовсе).
#
#   ./venv/bin/python -m ops.wb_jam_full_snapshot            # старт / продолжение
#   ./venv/bin/python -m ops.wb_jam_full_snapshot --status    # только показать прогресс
import sys, argparse, pathlib, time
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from collectors import wb_jam

ap = argparse.ArgumentParser()
ap.add_argument("--account", default="wb_acc1")
ap.add_argument("--days", type=int, default=7, help="окно Джема, дней")
ap.add_argument("--pause", type=float, default=None, help="пауза между запросами, с (по умолчанию из wb_jam)")
ap.add_argument("--limit", type=int, default=0, help="обработать не больше N товаров за запуск")
ap.add_argument("--status", action="store_true", help="показать прогресс и выйти")
A = ap.parse_args()

if A.pause:
    wb_jam.PAUSE = A.pause

cur, past = wb_jam._periods(A.days)
prog = pathlib.Path(f"/opt/mp-analytics/logs/jam_snapshot_{A.account}_{cur['start']}.done")
prog.parent.mkdir(exist_ok=True)
done = set()
if prog.exists():
    done = {int(x) for x in prog.read_text().split() if x.strip().isdigit()}

all_nm = [r["nm_id"] for r in db.query(
    "select distinct nm_id from wb_cards where account=%s order by nm_id", (A.account,))]
todo = [n for n in all_nm if n not in done]

print(f"каталог {A.account}: {len(all_nm)} товаров | обработано {len(done)} | осталось {len(todo)}")
print(f"окно Джема {cur['start']}…{cur['end']} | пауза {wb_jam.PAUSE} с | прогресс-файл {prog.name}")
if todo:
    print(f"оценка времени на остаток: {len(todo) * wb_jam.PAUSE / 3600:.1f} ч")
if A.status:
    sys.exit(0)

if A.limit:
    todo = todo[:A.limit]

t0 = time.time()
with prog.open("a") as fh:
    for i, nm in enumerate(todo, 1):
        try:
            wb_jam.fetch_search_texts(A.account, [nm], cur, past)
        except Exception as e:
            # Сетевой сбой — не помечаем обработанным, вернёмся к нему при следующем запуске.
            print(f"  [!] {nm}: {type(e).__name__} — пропуск без пометки", flush=True)
            time.sleep(10)
            continue
        fh.write(f"{nm}\n")
        fh.flush()
        if i % 50 == 0:
            sp = time.time() - t0
            eta = (len(todo) - i) * sp / i / 3600
            print(f"[слепок] {i}/{len(todo)} | прошло {sp/3600:.1f} ч | осталось ~{eta:.1f} ч", flush=True)
print(f"[слепок] готово за этот запуск: {len(todo)} товаров, {(time.time()-t0)/3600:.1f} ч")

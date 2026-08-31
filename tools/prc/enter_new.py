# поток: prc — добор карточек, заведённых сегодня, в актуальные оприходования поставщиков.
# Логика живёт в `prices/enter_add.py` — её же зовут кнопка «➕ карточка поставщика» и
# автозаведение; здесь только ручной запуск за день.
import sys, argparse, datetime as dt
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from prices import enter_add

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=str(dt.date.today()))
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

out, plan, skip = enter_add.report(day=a.day, dry=not a.apply, log=lambda *_: None)
print(open(out).read().split("\n## К добору")[0].rstrip())
print("отчёт:", out, "| режим:", "APPLY" if a.apply else "dry-run")

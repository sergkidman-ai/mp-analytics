# поток: card — применение миграций потока (600+)
import sys

sys.path.insert(0, "/opt/mp-analytics")
from core.db import apply_sql_file, query   # noqa: E402

for path in sys.argv[1:]:
    apply_sql_file(path)
    print("применено:", path)

for row in query("SELECT table_name, count(*) AS cols FROM information_schema.columns "
                 "WHERE table_name IN ('card_status', 'card_push_log') "
                 "GROUP BY 1 ORDER BY 1"):
    print(f"  {row['table_name']}: {row['cols']} колонок")

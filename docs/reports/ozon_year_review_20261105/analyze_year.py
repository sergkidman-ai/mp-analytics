# поток: mkt / ozon
"""Annual analysis entrypoint. Resume completed layers and use bounded-memory comparisons."""
import os
import pathlib
import sys
OUT=pathlib.Path(__file__).parent
ready=all((OUT/name).exists() for name in ["sku_positive_cpc_daily_year.csv","sku_general_sales_daily_year.csv","account_general_sales_daily_year.csv"])
entry=OUT/("analyze_year_streaming.py" if ready else "normalize_year.py")
os.execv(sys.executable,[sys.executable,str(entry)])

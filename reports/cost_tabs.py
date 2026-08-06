# поток: fin
"""reports/cost_tabs.py — подтабы раздела «Себестоимость» (одна полоса на все площадки).

Новые площадки/юрлица добавляются СЮДА, а не в левое меню: раздел один, подтабы горизонтальные.
"""

TABS = [
    ("ya",  "🟡 Яндекс Маркет",   "/reports/cost"),
    ("oz1", "🟦 Ozon · Цифровой", "/reports/cost/ozon/acc1"),
    ("oz2", "🟦 Ozon · Дисквэр",  "/reports/cost/ozon/acc2"),
    ("wb1", "🟣 WB · Цифровой",   "/reports/cost/wb/acc1"),
    ("wb2", "🟣 WB · Дисквэр",    "/reports/cost/wb/acc2"),
]


def tabs_html(cur):
    return "\n    ".join(
        f'<a class="rtab cur">{label}</a>' if key == cur else f'<a class="rtab" href="{href}">{label}</a>'
        for key, label, href in TABS)

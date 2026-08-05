# поток: fin
"""reports/cost_tabs.py — полоса подтабов раздела «Себестоимость» (одно место на весь раздел).

Подтабы живут ВНУТРИ раздела (левое меню не растёт): Яндекс.Маркет + два юрлица Ozon, у каждого
своя страница и свой провал в месяц. Юрлица не смешиваются — каждое своей таблицей.
"""

TABS = [
    ("ya",  "🟡 Яндекс Маркет",   "/reports/cost"),
    ("oz1", "🟦 Ozon · Цифровой", "/reports/cost/ozon/acc1"),
    ("oz2", "🟦 Ozon · Дисквэр",  "/reports/cost/ozon/acc2"),
]


def tabs_html(cur):
    """HTML полосы подтабов; cur — ключ активного таба ('ya' | 'oz1' | 'oz2')."""
    return "\n    ".join(
        f'<a class="rtab cur">{label}</a>' if key == cur
        else f'<a class="rtab" href="{href}">{label}</a>'
        for key, label, href in TABS)

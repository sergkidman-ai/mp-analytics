# поток: fin
"""reports/mp_tabs.py — подтабы раздела «Отчёты МП» (одна полоса на все страницы раздела).

Новые подразделы добавляются СЮДА, а не в три страницы по отдельности: полоса одна,
и раньше её приходилось править в ozon_mp_page, wb_mp_page и yandex_mp_page руками.
"""

TABS = [
    ("oz",  "🟦 Ozon",          "/reports"),
    ("wb",  "🟣 Wildberries",   "/reports/wb"),
    ("ya",  "🟡 Яндекс Маркет", "/reports/yandex"),
    ("fts", "🛃 Отчёты ФТС",    "/reports/fts"),
    ("upd", "📄 УПД по выкупам", "/reports/upd"),
]


def tabs_html(cur):
    return "\n    ".join(
        f'<a class="rtab cur">{label}</a>' if key == cur else f'<a class="rtab" href="{href}">{label}</a>'
        for key, label, href in TABS)

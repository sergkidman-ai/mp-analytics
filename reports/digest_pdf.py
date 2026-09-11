# поток: rev
"""reports/digest_pdf.py — дайджест в PDF (задача 11.09.2026, правка Сергея: PDF вместо md).

Зачем отдельный модуль. Markdown остаётся исходником — он читаем в git и его удобно
диффать, — но получателю нужен файл, который открывается на телефоне из Telegram как есть,
с таблицами и кликабельными ссылками на карточки. Поэтому PDF собирается НЕ из markdown,
а из тех же структур данных: так в таблицу попадает ровно то, что посчитано, без разбора
разметки посередине.

Кириллица — шрифтами DejaVu из системы (встроенные шрифты fpdf2 — latin-1 и на русском
рассыпаются). Ссылка на карточку кликабельная: fpdf2 кладёт её аннотацией PDF.
"""
import pathlib

from fpdf import FPDF

FONT_DIR = pathlib.Path("/usr/share/fonts/truetype/dejavu")
INK = (28, 28, 30)
MUTED = (110, 110, 118)
ACCENT = (150, 40, 40)
RULE = (216, 216, 222)
HEAD_BG = (238, 238, 242)


class Digest(FPDF):
    def __init__(self, title, subtitle):
        super().__init__(format="A4", unit="mm")
        self.title_text, self.subtitle = title, subtitle
        self.set_auto_page_break(True, margin=16)
        self.set_margins(15, 14, 15)
        # Начертания oblique в системе нет — под «курсив» подставляем засечки: название
        # товара всё равно должно отличаться от текста покупателя.
        for style, f in (("", "DejaVuSans.ttf"), ("B", "DejaVuSans-Bold.ttf"),
                         ("I", "DejaVuSerif.ttf")):
            self.add_font("dv", style, str(FONT_DIR / f))
        self.set_text_color(*INK)

    def footer(self):
        self.set_y(-12)
        self.set_font("dv", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 5, f"{self.title_text} · стр. {self.page_no()}", align="C")
        self.set_text_color(*INK)

    # ── блоки ──────────────────────────────────────────────────────────────
    def head(self):
        self.add_page()
        self.set_font("dv", "B", 17)
        self.multi_cell(0, 8, self.title_text, new_x="LMARGIN", new_y="NEXT")
        self.set_font("dv", "", 9)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 4.6, self.subtitle, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)
        self.ln(3)

    def h2(self, text):
        if self.get_y() > 240:
            self.add_page()
        self.ln(2)
        self.set_font("dv", "B", 12)
        self.multi_cell(0, 6.4, text, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*RULE)
        y = self.get_y() + 0.6
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(3)

    def h3(self, text, right=None):
        if self.get_y() > 250:
            self.add_page()
        self.ln(1.5)
        self.set_font("dv", "B", 10.5)
        self.set_text_color(*ACCENT)
        self.multi_cell(0, 5.6, text if not right else f"{text}   {right}",
                        new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)

    def note(self, text, italic=False):
        self.set_font("dv", "I" if italic else "", 8.5)
        self.set_text_color(*MUTED)
        self.multi_cell(0, 4.4, text, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)

    def para(self, text, size=9):
        self.set_font("dv", "", size)
        self.multi_cell(0, 4.8, text, new_x="LMARGIN", new_y="NEXT")

    def bullets(self, items, size=8.6):
        self.set_font("dv", "", size)
        for it in items:
            self.multi_cell(0, 4.4, "• " + it, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def table(self, header, rows, widths):
        """Простая таблица: шапка серой заливкой, строки по переносу текста."""
        if not rows:
            return
        self.set_font("dv", "B", 8.6)
        self.set_fill_color(*HEAD_BG)
        for w, h in zip(widths, header):
            self.cell(w, 6, " " + h, border=0, fill=True)
        self.ln(6)
        self.set_font("dv", "", 8.4)
        self.set_draw_color(*RULE)
        for row in rows:
            heights = []
            for w, cell in zip(widths, row):
                heights.append(len(self.multi_cell(w - 2, 4.3, str(cell), dry_run=True,
                                                   output="LINES")) * 4.3)
            h = max(heights + [5.4])
            if self.get_y() + h > self.h - 18:
                self.add_page()
            y0 = self.get_y()
            x = self.l_margin
            for w, cell in zip(widths, row):
                self.set_xy(x, y0)
                self.multi_cell(w - 2, 4.3, str(cell), new_x="LMARGIN", new_y="TOP")
                x += w
            self.set_xy(self.l_margin, y0 + h)
            self.line(self.l_margin, self.get_y() - 0.8, self.w - self.r_margin,
                      self.get_y() - 0.8)
        self.ln(2)

    def card_links(self, items):
        """Кликабельные ссылки на карточки — по одной на площадку."""
        if not items:
            return
        self.set_font("dv", "", 8)
        self.set_text_color(40, 80, 170)
        for i, lk in enumerate(items):
            label = f"{lk['platform']}" + (f" · {lk['article']}" if lk.get("article") else "")
            self.cell(self.get_string_width(label) + 3, 4.6, label, link=lk["url"])
        self.ln(5)
        self.set_text_color(*INK)


def _stars(v):
    return "—" if v is None else "★" * int(v)


def content_pdf(data, path):
    d = Digest(f"Дайджест контентщику — {data['day']}",
               f"Окно: последние {data['days']} дн. Источник — raw_feedback, без ИИ: счёт по темам "
               f"вопросов.\nКаждая строка — вопрос, на который карточка обязана была ответить сама. "
               f"Пока она молчит, за неё отвечает оператор — и так по кругу.\n"
               f"Артикул — базовый, номенклатуры: площадочные коды всех витрин и аккаунтов сведены "
               f"в один.")
    d.head()

    d.h2(f"1. Карточка не отвечает на вопрос (≥{data.get('min_hits', 2)} вопроса одной темы)")
    if not data["gaps"]:
        d.note("За окно таких артикулов нет.")
    for g in data["gaps"]:
        d.h3(f"{g['article']} — {g['topic']} ×{g['n']}")
        if g.get("product"):
            d.note(g["product"], italic=True)
        d.set_font("dv", "B", 8.6)
        d.multi_cell(0, 4.6, "Что добавить: " + g["advice"], new_x="LMARGIN", new_y="NEXT")
        d.bullets(g["texts"])
        d.card_links(g.get("links") or [])

    d.h2("2. Отзывы «на фото другое / не как в описании»")
    if not data["mismatch"]:
        d.note("За окно таких отзывов нет.")
    else:
        d.table(["Артикул", "Пл.", "★", "Отзыв"],
                [[m["article"], m["platform"], _stars(m.get("rating")), m["text"]]
                 for m in data["mismatch"]], [24, 16, 14, 126])
        for m in data["mismatch"]:
            d.card_links(m.get("links") or [])

    d.h2("3. Модели принтера — кандидаты в заголовок")
    d.note("Спрошено покупателем, ответ утвердительный («да, подойдёт»), утверждён человеком, "
           "а в списке моделей карточки модели нет. Полярность ответа проверяется: отказы "
           "оператор утверждает точно так же, и раньше они сюда попадали.")
    d.ln(1)
    if data.get("titles_note"):
        d.note(data["titles_note"])
    if not data["titles"]:
        d.note("За окно таких моделей нет.")
    for t in data["titles"]:
        d.h3(f"{t['article']} → добавить: " + ", ".join(t["models"]))
        if t.get("product"):
            d.note(t["product"], italic=True)
        d.bullets([f"вопрос: {t['question']}"])
        d.card_links(t.get("links") or [])

    d.output(str(path))
    return path


def purchase_pdf(data, path):
    d = Digest(f"Дайджест закупщику — {data['day']}",
               f"Окно: последние {data['days']} дн. Источник — raw_feedback, без ИИ: счёт по "
               f"симптомам.\nВ список попадают только претензии — то же определение, что у движка "
               f"модерации.\nПретензий «после заправки» отсеяно: {data['skipped_refill']} — это не "
               f"наш дефект.\nАртикул — базовый, номенклатуры: повторы считаются через все "
               f"площадки и аккаунты.")
    d.head()

    d.h2("⚠ Срез по цвету (за 30 дн., разные артикулы)")
    if not data["colors"]:
        d.note("За 30 дней ни один цвет не набрал 3 претензий по разным артикулам.")
    for c in data["colors"]:
        share = round(100 * c["n"] / max(c["base"], 1))
        d.h3(f"{c['color']} — {c['n']} претензий на {len(c['articles'])} артикулах "
             f"({c['n']} из {c['base']} отзывов по цвету, {share} %)")
        d.note("Сравнивать надо доли: чёрного продаётся больше всех, и по абсолютному числу "
               "претензий он будет первым всегда.")
        d.para("Артикулы: " + ", ".join(c["articles"]), size=8.6)
        d.table(["Артикул", "★", "Текст"],
                [[t["article"], _stars(t.get("rating")), t["text"]] for t in c["texts"]],
                [24, 16, 140])

    d.h2(f"Артикулы с повторяющимся симптомом (≥{data.get('min_hits', 2)} за окно)")
    if not data["symptoms"]:
        d.note("За окно таких артикулов нет.")
    for s in data["symptoms"]:
        d.h3(f"{s['article']} — {s['symptom']} ×{s['n']}")
        if s.get("product"):
            d.note(s["product"], italic=True)
        d.para("Оценки: " + (" ".join(_stars(x) for x in s["stars"]) or "—"), size=8.6)
        d.table(["Пл.", "Тип", "★", "Текст"],
                [[t["platform"], "вопрос" if t["kind"] == "question" else "отзыв",
                  _stars(t.get("rating")), t["text"]] for t in s["texts"]],
                [16, 18, 14, 132])
        d.card_links(s.get("links") or [])

    d.output(str(path))
    return path

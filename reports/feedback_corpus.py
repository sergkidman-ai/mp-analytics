"""reports/feedback_corpus.py — справочник НАШИХ ответов как база для LLM (RAG).

Наши ручные ответы (raw_feedback.answer_text, сейчас WB — там текст сохранён) — лучший источник
тона и фактуры. Здесь: разметка по интенту, индекс, ретрив похожих прошлых Q&A под каждый новый
элемент (динамические few-shot для LLM) и выгрузка справочника в HTML.

Запуск:  ./venv/bin/python reports/feedback_corpus.py       # собрать и выгрузить справочник
"""
import re
import sys
import html
import pathlib
from collections import Counter

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                          # noqa: E402

REF_OUT = BASE_DIR / "docs" / "feedback_reference.html"

INTENTS = [
    ("совместимость модели", r"подойд|подход|совмест|для како|модел|к принтер"),
    ("оригинал/совместимый", r"оригинал|аналог|подделк|фирменн"),
    ("заправка/тонер", r"заправл|тонер|краск|чернил|порош"),
    ("чип/не читается", r"\bчип|не вид|не опозна|не распозна|ошибк"),
    ("ресурс/объём", r"ресурс|хват|стран|сколько|\bмл\b|грамм|объ[её]м"),
    ("цвет", r"цвет|чёрн|черн|цветн|пурпур|голуб|жёлт|желт"),
    ("установка", r"установ|вставл|как поставить|снять|колпач"),
    ("возврат/брак", r"верн|возврат|обмен|брак|течёт|течет|сломал"),
    ("доставка/упаковка", r"доставк|привез|упаковк|коробк|помят"),
    ("качество печати", r"печат|полос|бледн|светл|мажет|развод"),
]
BRANDS = ["hp", "canon", "epson", "samsung", "brother", "kyocera", "xerox", "ricoh",
          "pantum", "panasonic", "oki", "lexmark", "toshiba", "sharp"]


def intent(text):
    t = (text or "").lower()
    for name, rx in INTENTS:
        if re.search(rx, t):
            return name
    return "прочее"


def _family(product_name, body=""):
    t = (str(product_name or "") + " " + str(body or "")).lower()
    for b in BRANDS:
        if b in t:
            return b
    return None


_STOP = set("и в на для с по не что как это к у из the на вы да нет ли же бы под до от а о при или это"
            .split())


def _tokens(text):
    return {w for w in re.findall(r"[а-яёa-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


class Corpus:
    def __init__(self, rows):
        self.items = []
        for r in rows:
            src = (r["body"] or r["pros"] or r["cons"] or "").strip()
            self.items.append({
                "kind": r["kind"], "rating": r["rating"], "product": r["product_name"],
                "src": src, "answer": r["answer_text"].strip(),
                "intent": intent(src if r["kind"] == "question" else (src or "отзыв")),
                "family": _family(r["product_name"], src), "tok": _tokens(src)})

    def retrieve(self, kind, src, product, k=5):
        it_intent = intent(src) if kind == "question" else intent(src or "отзыв")
        it_family = _family(product, src)
        it_tok = _tokens(src)
        scored = []
        for c in self.items:
            if c["kind"] != kind:
                continue
            s = 0
            if c["intent"] == it_intent:
                s += 3
            if it_family and c["family"] == it_family:
                s += 3
            s += len(it_tok & c["tok"])
            if s > 0 and c["answer"]:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:k]]


def load_corpus():
    rows = db.query("""SELECT kind,rating,product_name,body,pros,cons,answer_text FROM raw_feedback
        WHERE is_answered AND answer_text IS NOT NULL AND length(trim(answer_text))>0""")
    return Corpus(rows)


# ─────────────────────────── выгрузка справочника ───────────────────────────
def _esc(s):
    return html.escape(str(s or ""))


def export_reference(per_intent=8):
    c = load_corpus()
    q = [it for it in c.items if it["kind"] == "question"]
    rv = [it for it in c.items if it["kind"] == "review"]
    css = """body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f4f5f7;color:#1a1a1a}
    @media(prefers-color-scheme:dark){body{background:#161719;color:#e6e6e6}.b{background:#1f2124!important;border-color:#2c2f33!important}}
    header{padding:20px 28px;background:#1f7a4d;color:#fff}h1{margin:0;font-size:19px}.sub{opacity:.85;font-size:13px;margin-top:4px}
    .wrap{max-width:1000px;margin:0 auto;padding:18px}h2{margin:22px 0 8px;font-size:15px;border-bottom:2px solid #cbd5cd;padding-bottom:4px}
    .b{background:#fff;border:1px solid #e3e5e8;border-radius:9px;padding:10px 13px;margin:8px 0}
    .q{font-size:13px;color:#555}@media(prefers-color-scheme:dark){.q{color:#aaa}}
    .a{font-size:14px;margin-top:5px;border-left:3px solid #1f7a4d;padding-left:9px;white-space:pre-wrap}
    .p{font-size:11px;color:#999}.cnt{font-size:12px;color:#1f7a4d;font-weight:700}"""
    parts = [f"<style>{css}</style><header><h1>Справочник наших ответов</h1>"
             f"<div class='sub'>{len(c.items)} ответов с текстом (WB) · база тона и фактуры для LLM · "
             f"сгруппировано по теме</div></header><div class='wrap'>"]
    for title, pool in (("Вопросы", q), ("Отзывы", rv)):
        parts.append(f"<h2>{title} — {len(pool)}</h2>")
        by = {}
        for it in pool:
            by.setdefault(it["intent"], []).append(it)
        for name, rs in sorted(by.items(), key=lambda x: -len(x[1])):
            parts.append(f"<div class='cnt'>{_esc(name)} — {len(rs)}</div>")
            for it in rs[:per_intent]:
                src = it["src"] or f"({it['rating']}★ без текста)"
                parts.append(f"<div class='b'><div class='p'>{_esc(it['product'])}</div>"
                             f"<div class='q'>❓ {_esc(src[:140])}</div>"
                             f"<div class='a'>{_esc(it['answer'][:260])}</div></div>")
    parts.append("</div>")
    REF_OUT.write_text("".join(parts), encoding="utf-8")
    print(f"Справочник: {len(c.items)} ответов, вопросов {len(q)}, отзывов {len(rv)}", flush=True)
    print(f"Интенты вопросов: {dict(Counter(i['intent'] for i in q))}", flush=True)
    print(f"Файл: {REF_OUT}", flush=True)
    return c


if __name__ == "__main__":
    export_reference()

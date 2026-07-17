"""reports/feedback_sample.py — ПРОГОН движка на СЛУЧАЙНОЙ выборке вопросов «в разное время» + отзывы.

Берёт N случайных вопросов, стратифицированных по месяцам (temporal spread — не только свежак),
и M случайных отзывов из всей истории. Гонит их через ПОЛНЫЙ движок (reports.feedback_today._answer:
карточка v2 + семья/вариант серии + веб + каталог + few-shot). Где вопрос уже был отвечён нами —
показывает НАШ реальный ответ рядом (сверка). Ничего не постит и не пишет draft_* в БД.

Запуск:  ./venv/bin/python reports/feedback_sample.py [n_questions] [m_reviews]
"""
import sys
import html
import pathlib
import warnings
from collections import Counter

warnings.filterwarnings("ignore")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")
from core import db                                                     # noqa: E402
from reports.feedback_today import _answer, _client                     # noqa: E402
from reports.card_facts import CardFacts                                # noqa: E402
from reports.feedback_corpus import load_corpus                         # noqa: E402

ART = BASE_DIR / "docs" / "feedback_sample_artifact.html"


def pick_questions(n):
    # до 2 случайных вопросов на КАЖДЫЙ месяц истории → гарантированный разброс по времени
    return db.query("""
        SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
               answer_text,created_at, to_char(created_at,'YYYY-MM') ym FROM (
          SELECT *, row_number() OVER (PARTITION BY date_trunc('month',created_at) ORDER BY random()) rn
          FROM raw_feedback
          WHERE kind='question' AND account IN ('wb_acc1','oz_acc1')
            AND length(trim(coalesce(body,'')))>15) t
        WHERE rn<=2 ORDER BY random() LIMIT %s""", (n,))


def pick_reviews(m):
    return db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
        answer_text,created_at, to_char(created_at,'YYYY-MM') ym FROM raw_feedback
        WHERE kind='review' AND account IN ('wb_acc1','oz_acc1')
        AND length(trim(coalesce(body,'')||coalesce(pros,'')))>8 ORDER BY random() LIMIT %s""", (m,))


def main(nq=20, mr=8):
    cf, corpus, client = CardFacts(), load_corpus(), _client()
    qs = pick_questions(nq)
    rvs = pick_reviews(mr)
    print(f"Выборка: {len(qs)} вопросов (в разное время) + {len(rvs)} отзывов. Корпус few-shot: {len(corpus.items)}.",
          flush=True)
    out = []
    for r in list(qs) + list(rvs):
        outd, *_rest = _answer(client, r, cf, corpus)
        outd["answer_text"] = r.get("answer_text")
        outd["ym"] = r.get("ym")
        out.append(outd)
        if r["kind"] == "question":
            print(f"· {r['ym']} {r['platform']} · {outd['intent']} · src={outd['source'] or '—'}", flush=True)
            print("   Q:", (r["body"] or "")[:110].replace("\n", " "), flush=True)
            print("   →:", (outd["reply"] or "")[:170].replace("\n", " "), flush=True)
            if r.get("answer_text"):
                print("   МЫ:", (r["answer_text"] or "")[:170].replace("\n", " "), flush=True)
    _html(out)
    src = Counter(o["source"] for o in out if o["cat"] == "question")
    print(f"\nИТОГ: {len([o for o in out if o['cat']=='question'])} вопросов "
          f"(источники: {dict(src)}) + {len([o for o in out if o['cat'].startswith('review')])} отзывов. "
          f"Артефакт-файл: {ART}", flush=True)
    return out


def _e(s):
    return html.escape(str(s or ""))


def _card(o):
    q = o["cat"] == "question"
    real = (f'<div class="real"><span class="lbl2">наш реальный ответ ({_e(o["ym"])})</span>{_e(o["answer_text"])[:400]}</div>'
            if o.get("answer_text") else ('<div class="real muted">— мы на этот вопрос ещё не отвечали</div>' if q else ""))
    src = o.get("source") or ("шаблон" if o["cat"] == "review-empty" else "карточка")
    scls = {"веб": "s-web", "карточка-серия": "s-fam"}.get(src, "s-card")
    links = ""
    if o.get("sources"):
        li = "".join(f'<li><a href="{_e(s.get("url"))}" target="_blank" rel="noopener">{_e(s.get("title") or s.get("url"))[:70]}</a></li>'
                     for s in o["sources"][:4])
        links = f'<div class="links"><span class="lbl2">веб-источники</span><ul>{li}</ul></div>'
    tag = (o["intent"] or "вопрос") if q else f"отзыв {o.get('rating') or ''}★"
    qtext = _e(o["body"] or o["pros"] or "(без текста)")
    return f"""<div class="item">
 <div class="ihead"><span class="plat">{_e(o['platform'])}</span><span class="date">{_e(o.get('ym'))}</span>
   <span class="tag">{_e(tag)}</span><span class="chip {scls}">{_e(src)}</span>
   <span class="badge b-review">черновик</span></div>
 <div class="q">{qtext}</div>
 <div class="prod muted">{_e(o['product_name'])[:70]}</div>
 <div class="reply"><span class="lbl2">черновик движка</span>{_e(o['reply'])}</div>
 {links}{real}
 <div class="note muted">источник: {_e(src)} · grounded={str(o['grounded']).lower()} · {_e(o['note'])[:170]}</div>
</div>"""


def _html(out):
    qs = [o for o in out if o["cat"] == "question"]
    rvs = [o for o in out if o["cat"].startswith("review")]
    src = Counter(o["source"] or "карточка" for o in qs)
    style = """:root{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
@media(prefers-color-scheme:dark){:root{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}}
:root[data-theme="dark"]{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}
:root[data-theme="light"]{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
*{box-sizing:border-box}body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.wrap{max-width:900px;margin:0 auto;padding:26px 20px 48px}.eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
h1{font-size:24px;margin:0 0 6px}.sub{color:var(--muted);margin:0 0 18px;max-width:74ch}
.tiles{display:flex;gap:12px;margin:16px 0;flex-wrap:wrap}.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;flex:1;min-width:110px}
.tile .n{font-size:24px;font-weight:750;font-variant-numeric:tabular-nums}.tile .k{font-size:12px;color:var(--muted);margin-top:4px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.item{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin:12px 0}
.ihead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}.plat{font-weight:650;text-transform:capitalize}
.date{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}.tag{font-size:11.5px;color:var(--muted)}
.chip{font-size:11px;font-weight:650;color:var(--chipink);background:var(--chip);padding:2px 8px;border-radius:20px}
.chip.s-web{color:var(--auto);background:var(--auto-bg)}.chip.s-fam{color:var(--review);background:var(--review-bg)}
.links{margin-top:8px}.links ul{margin:4px 0 0;padding-left:18px}.links li{font-size:12px;margin:2px 0}.links a{color:var(--accent)}
.badge{margin-left:auto;display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750}.b-review{color:var(--review);background:var(--review-bg)}
.q{font-weight:550;margin:2px 0}.prod{font-size:12px;margin:2px 0 10px}
.reply{background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;padding:10px 12px;font-size:14px}
.real{margin-top:8px;padding:9px 12px;border:1px dashed var(--dash);border-radius:8px;font-size:13px}
.lbl2{display:block;font-size:10.5px;font-weight:750;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);margin-bottom:4px}
.real .lbl2{color:var(--auto)}.note{font-size:11.5px;margin-top:8px}.muted{color:var(--muted)}
.foot{color:var(--muted);font-size:13px;margin-top:24px;max-width:78ch;border-top:1px dashed var(--dash);padding-top:14px}"""
    tiles = "".join(f'<div class="tile"><div class="n">{n}</div><div class="k">источник: {_e(k)}</div></div>'
                    for k, n in src.most_common())
    body = f"""<div class="wrap"><p class="eyebrow">Цифровой квадрат · случайная выборка «в разное время»</p>
<h1>Движок на 20 случайных вопросах + отзывы</h1>
<p class="sub">Вопросы выбраны случайно и стратифицированы по месяцам (разные периоды, не только свежак).
Прогон полным движком: карточка (card_facts v2) → вариант серии → веб-поиск → каталог, few-shot из наших
ответов. Где вопрос уже был отвечён — рядом наш реальный ответ для сверки. <b>Черновики, ничего не опубликовано.</b></p>
<div class="tiles"><div class="tile"><div class="n">{len(qs)}</div><div class="k">вопросов</div></div>
<div class="tile"><div class="n">{len(rvs)}</div><div class="k">отзывов</div></div>{tiles}</div>
<h2>Вопросы — {len(qs)}</h2>{''.join(_card(o) for o in qs)}
<h2>Отзывы — {len(rvs)}</h2>{''.join(_card(o) for o in rvs)}
<p class="foot">Источник ответа помечен бейджем: <b>карточка</b>/<b>карточка-серия</b> (вариант линейки,
бесплатно) · <b>веб</b> (внешний поиск со ссылками) · <b>каталог</b> (наши листинги) · <b>шаблон</b> (отзывы).
QR для чата — на упаковке и в товарном чеке внутри коробки.</p></div>"""
    ART.write_text(f'<title>Случайная выборка ответов движка — Цифровой квадрат</title>\n<style>{style}</style>\n{body}',
                   encoding="utf-8")


if __name__ == "__main__":
    nq = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    mr = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    main(nq, mr)

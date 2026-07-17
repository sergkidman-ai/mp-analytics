"""reports/feedback_llm_sync.py — СИНХРОННЫЙ прогон ИИ-слоя со сверкой (для быстрой оценки).

Не батч (мгновенно): гоняет N вопросов через messages.create по relay, показывает ответ модели
рядом с нашим РЕАЛЬНЫМ историческим ответом. Берёт отвеченные вопросы (стратифицировано по интентам)
+ живой неотвеченный backlog. Ничего не постит. Печатает в консоль + пишет artifact HTML.

Запуск:  ./venv/bin/python reports/feedback_llm_sync.py [N]
"""
import os
import re
import sys
import json
import html
import warnings
import pathlib
from collections import Counter

warnings.filterwarnings("ignore")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")
from core import db                                                    # noqa: E402
from reports.feedback_llm import _card_data, _user_block, _name, SYSTEM, MODEL, _text_of  # noqa: E402
from reports.card_facts import CardFacts                               # noqa: E402
from reports.feedback_corpus import load_corpus, intent                # noqa: E402

ART = BASE_DIR / "docs" / "feedback_llm_sync_artifact.html"


def pick(n_ans=18):
    rows = []
    # живой неотвеченный backlog вопросов (свежие)
    rows += db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
        NULL::text answer_text FROM raw_feedback WHERE kind='question' AND is_answered=false
        AND length(trim(coalesce(body,'')))>10 ORDER BY created_at DESC LIMIT 4""")
    # отвеченные: по 2 на интент
    ans = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
        answer_text FROM raw_feedback WHERE kind='question' AND is_answered
        AND coalesce(trim(answer_text),'')<>'' AND length(trim(coalesce(body,'')))>15
        ORDER BY ext_id DESC""")
    seen = Counter()
    for r in ans:
        k = intent(r["body"])
        if seen[k] >= 2:
            continue
        seen[k] += 1
        rows.append(r)
        if len([x for x in rows if x["answer_text"]]) >= n_ans:
            break
    return rows


def call(client, r, cf, corpus):
    cc = _card_data(r, cf)
    ex = corpus.retrieve(r["kind"], r["body"] or "", r["product_name"], k=5)
    content = _user_block(r, _name(r), cc, ex)
    m = client.messages.create(model=MODEL, max_tokens=600, system=SYSTEM,
                               messages=[{"role": "user", "content": content}])
    raw = _text_of(m)
    try:
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
    except Exception:
        data = {"reply": raw[:400], "route": "review", "confidence": 0, "grounded": False, "note": "parse-fail"}
    return data, cc


def main(n=20):
    from anthropic import Anthropic
    import httpx
    base = os.environ.get("ANTHROPIC_BASE_URL")
    client = (Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=base,
                        http_client=httpx.Client(verify=False)) if base
              else Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]))
    cf, corpus = CardFacts(), load_corpus()
    rows = pick()[:n]
    out = []
    for i, r in enumerate(rows, 1):
        try:
            data, cc = call(client, r, cf, corpus)
        except Exception as e:
            data, cc = {"reply": f"[ошибка вызова: {str(e)[:120]}]", "route": "review",
                        "confidence": 0, "grounded": False, "note": ""}, ""
        out.append(dict(r, **{"m_reply": data.get("reply", ""), "m_route": data.get("route", "review"),
                              "m_conf": data.get("confidence", 0), "m_grounded": data.get("grounded", False),
                              "m_note": data.get("note", ""), "intent": intent(r["body"]), "card": cc}))
        st = "новый" if not r["answer_text"] else "сверка"
        print(f"\n[{i}/{len(rows)}] {r['platform']} · {intent(r['body'])} · {st}")
        print("  ВОПРОС:", (r["body"] or "")[:150].replace("\n", " "))
        print("  МОДЕЛЬ:", (data.get("reply") or "")[:240].replace("\n", " "),
              f"  ({data.get('route')}/conf {data.get('confidence')}/grounded {data.get('grounded')})")
        if r["answer_text"]:
            print("  МЫ:    ", (r["answer_text"] or "")[:240].replace("\n", " "))
    _html(out)
    ca = Counter(o["m_route"] for o in out)
    print(f"\nИТОГ: {len(out)} вопросов · auto {ca['auto']} / review {ca['review']} · артефакт-файл {ART}")
    return out


def _e(s):
    return html.escape(str(s or ""))


def _html(out):
    rows = []
    for o in out:
        rb = ("АВТО", "b-auto") if o["m_route"] == "auto" else ("РЕВЬЮ", "b-review")
        real = f'<div class="real"><span class="lbl">наш реальный ответ:</span> {_e(o["answer_text"])[:300]}</div>' if o["answer_text"] else '<div class="real muted">живой backlog — реального ответа ещё нет</div>'
        rows.append(f"""<tr>
 <td class="plat">{o['platform']}<div class="muted" style="font-size:11px">{_e(o['intent'])}</div></td>
 <td class="q">{_e(o['body'])[:180]}<div class="prod muted">{_e(o['product_name'])[:60]}</div></td>
 <td class="ans"><div class="myans">{_e(o['m_reply'])}</div>{real}
   <div class="src muted">conf {o['m_conf']} · grounded {str(o['m_grounded']).lower()} · {_e(o['m_note'])[:90]}</div></td>
 <td><span class="badge {rb[1]}">{rb[0]}</span></td></tr>""")
    ca = Counter(o["m_route"] for o in out)
    style = """:root{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--accent-soft:#e2eef2;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--dash:#dbe1e7}
@media(prefers-color-scheme:dark){:root{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--accent-soft:#123039;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--dash:#2b333d}}
:root[data-theme="dark"]{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--accent-soft:#123039;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--dash:#2b333d}
:root[data-theme="light"]{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--accent-soft:#e2eef2;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--dash:#dbe1e7}
*{box-sizing:border-box}body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.wrap{max-width:1080px;margin:0 auto;padding:26px 20px 44px}.eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}.sub{color:var(--muted);margin:0 0 18px;max-width:72ch}
.tiles{display:flex;gap:12px;margin:16px 0}.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;flex:1}
.tile .n{font-size:26px;font-weight:750;font-variant-numeric:tabular-nums}.tile.auto .n{color:var(--auto)}.tile.review .n{color:var(--review)}.tile .k{font-size:12px;color:var(--muted);margin-top:4px}
.scroll{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--surface)}table{border-collapse:collapse;width:100%;min-width:820px}
th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--border);vertical-align:top}tr:last-child td{border-bottom:none}
th{background:var(--surface2);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700}
tbody tr:hover{background:var(--surface2)}.plat{font-weight:650;text-transform:capitalize;white-space:nowrap}
.q{max-width:280px;font-weight:500}.prod{font-size:11.5px;margin-top:3px;font-weight:400}.ans{max-width:440px}.myans{font-weight:500}
.real{margin-top:8px;padding-top:8px;border-top:1px dashed var(--dash);font-size:12.5px}.real .lbl{color:var(--auto);font-weight:650}
.src{margin-top:6px;font-size:11.5px}.muted{color:var(--muted)}
.badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750;white-space:nowrap}.b-auto{color:var(--auto);background:var(--auto-bg)}.b-review{color:var(--review);background:var(--review-bg)}
.foot{color:var(--muted);font-size:13px;margin-top:18px;max-width:76ch}"""
    body = f"""<div class="wrap"><p class="eyebrow">Цифровой квадрат · ИИ-слой (claude-sonnet-5)</p>
<h1>Ответы модели со сверкой</h1>
<p class="sub">Синхронный прогон ИИ-слоя на реальных вопросах. Факты — из карточки (card_facts v2),
few-shot — из 11 995 наших ответов. Рядом — наш реальный исторический ответ. Режим только черновики.</p>
<div class="tiles"><div class="tile auto"><div class="n">{ca['auto']}</div><div class="k">route=auto</div></div>
<div class="tile review"><div class="n">{ca['review']}</div><div class="k">route=review</div></div>
<div class="tile"><div class="n">{len(out)}</div><div class="k">вопросов в прогоне</div></div></div>
<div class="scroll"><table><thead><tr><th>Пл./интент</th><th>Вопрос</th><th>Ответ модели / сверка</th><th>Маршрут</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="foot">Модель отвечает по фактам карточки; где данных нет или спорная совместимость — route=review
(на человека). В фазе «только черновики» вопросы всегда помечаются на вычитку.</p></div>"""
    ART.write_text(f'<title>ИИ-ответы со сверкой — Цифровой квадрат</title>\n<style>{style}</style>\n{body}',
                   encoding="utf-8")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)

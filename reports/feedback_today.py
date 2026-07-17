"""reports/feedback_today.py — ПРОГОН ЧЕРНОВИКОВ на свежих необработанных отзывах и вопросах.

Забирает необработанное (is_answered=false) за последние дни и генерит черновики ВСЕМ функционалом:
  ВОПРОСЫ + отзывы С ТЕКСТОМ → ИИ-слой (Claude через relay): факты card_facts v2 (WB-модели из
    описания, WB-чип из Ozon-двойника) + каталог наших листингов + few-shot из корпуса наших ответов.
  ПУСТЫЕ отзывы (5★ без текста) → детерминированный шаблон (без токенов).
Сохраняет черновики в raw_feedback.draft_* и собирает один артефакт docs/feedback_today_artifact.html.
НИЧЕГО НЕ ПОСТИТ на площадках — только черновики.

Запуск:  ./venv/bin/python reports/feedback_today.py [--since YYYY-MM-DD]
"""
import os
import re
import sys
import json
import html
import pathlib
import warnings
from collections import Counter

warnings.filterwarnings("ignore")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")
from core import db                                                              # noqa: E402
from reports.feedback_llm import (_card_data, _user_block, _name, SYSTEM, MODEL,  # noqa: E402
                                  _text_of, _asked_models)
from reports.feedback_drafts import _norm                                        # noqa: E402
from reports.card_facts import CardFacts                                         # noqa: E402
from reports.feedback_corpus import load_corpus, intent                         # noqa: E402
from reports.feedback_draft_run import draft_review, _first_name, _short, DEFECT_RX  # noqa: E402
from reports.feedback_web import web_compat                                      # noqa: E402

# сигнал, что товар УЖЕ куплен/используется (тогда уместен QR на упаковке/чеке); иначе — пред-продажа.
# Широко: покупка + любой признак использования/поломки (печатает бело/пусто, «что делать», выдаёт ошибку).
_OWNED_RX = re.compile(
    r"куп(и|л|ил|лен)|приобре|заказал|пришёл|пришел|получил|установил|поставил|вставил|"
    r"распечат|не\s+печат|не\s+вид|не\s+опозна|ошибк|брак|верну|возврат|замен|сломал|течёт|течет|"
    r"мажет|полос|что\s+делать|выдаёт|выдает|пишет\b|горит|мига|перестал|не\s+работа|"
    r"(?:бел|пуст|чист|сер)\w*\s+лист|печатает\s+(?:пусто|бел|плохо|бледно|сер)", re.I)
_QR_RX = re.compile(r"(?:по\s*)?QR[-\s]?код\w*"
                    r"(?:\s*(?:внутри|товарн\w*|чек\w*|упаковк\w*|коробк\w*|стикер\w*|или|на|и|в|,))+", re.I)

ART = BASE_DIR / "docs" / "feedback_today_artifact.html"


def _base_model(m):
    """База модели: обрезаем короткий буквенный суффикс варианта серии (CX17NF→CX17, C1750N→C1750)."""
    s = re.sub(r"[^a-z0-9]", "", (m or "").lower())
    return re.sub(r"([0-9])[a-z]{1,4}$", r"\1", s)


def _fam_status(question, card_models):
    """Совместимость с учётом ВАРИАНТОВ серии. → ('yes',matched)|('unknown',asked)|('no_data'|'no_ask',[])."""
    asked = _asked_models(question)
    if not card_models:
        return ("no_data", [])
    if not asked:
        return ("no_ask", [])
    cn = [_norm(m) for m in card_models]
    cb = [_base_model(m) for m in card_models]
    matched = []
    for a in asked:
        na, ba = _norm(a), _base_model(a)
        if any(na in c or c in na for c in cn) or (len(ba) >= 3 and ba in cb):
            matched.append(a)
    return ("yes", matched) if matched else ("unknown", asked)


def _gather(since):
    # необработанные за окно (последний месяц) — и вопросы, и отзывы по дате created_at
    q = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
        created_at FROM raw_feedback WHERE is_answered=false AND account IN ('wb_acc1','oz_acc1')
        AND created_at >= %s ORDER BY (kind='question') DESC, created_at DESC NULLS LAST""",
        (since,))
    return q


def _has_text(r):
    return len((r["body"] or "") + (r["pros"] or "") + (r["cons"] or "")) > 3


def _client():
    from anthropic import Anthropic
    import httpx
    key = os.environ["ANTHROPIC_API_KEY"]
    base = os.environ.get("ANTHROPIC_BASE_URL")
    return (Anthropic(api_key=key, base_url=base, http_client=httpx.Client(verify=False))
            if base else Anthropic(api_key=key))


def _llm(client, r, cf, corpus):
    """ИИ-черновик: карточка+каталог+few-shot → JSON {reply,route,confidence,grounded,note}."""
    cc = _card_data(r, cf)
    ex = corpus.retrieve(r["kind"], r["body"] or r["pros"] or r["cons"] or "", r["product_name"], k=5)
    content = _user_block(r, _name(r), cc, ex)
    m = client.messages.create(model=MODEL, max_tokens=800, system=SYSTEM,
                               messages=[{"role": "user", "content": content}])
    raw = _text_of(m)
    d = None
    mm = re.search(r"\{.*\}", raw, re.S)
    if mm:
        try:
            d = json.loads(mm.group(0))
        except Exception:
            d = None
    if d is None:                                    # JSON битый (обрезка) — спасаем текст reply регексом
        rep = re.search(r'"reply"\s*:\s*"(.+?)"\s*,\s*"route"', raw, re.S)
        reply_txt = rep.group(1).replace('\\"', '"').replace("\\n", " ") if rep else raw[:400]
        d = {"reply": reply_txt, "route": "review", "confidence": 0, "grounded": False, "note": "parse-salvage"}
    # guardrail совместимости: утвердительное «да, подойдёт» без модели в карточке → review
    if r["kind"] == "question":
        asked = _asked_models(r["body"])
        aff = re.search(r"подойд|подход|совмест|да,? ", (d.get("reply") or "").lower())
        cn = _norm(cc or "")
        if asked and aff and cn and not any(_norm(a) in cn for a in asked):
            d["route"], d["grounded"] = "review", False
            d["note"] = "guardrail: совместимость не подтверждена карточкой; " + (d.get("note") or "")
        d["route"] = "review"                      # фаза «только черновики»: вопросы всегда на вычитку
    return d, cc


def _store(r, reply, route, conf, ground):
    from psycopg2.extras import Json
    db.execute("""UPDATE raw_feedback SET draft_text=%s, draft_route=%s, draft_confidence=%s,
        draft_category=%s, draft_grounding=%s, draft_at=now()
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
        (reply, route, conf, ("question" if r["kind"] == "question" else "review"),
         Json(ground), r["platform"], r["account"], r["kind"], r["ext_id"]))


def _presale_scrub(reply, r):
    """Пред-продажный вопрос: у покупателя нет коробки/чека — убираем ссылку на QR на упаковке/в чеке."""
    if r["kind"] != "question":
        return reply
    body = r["body"] or ""
    if _OWNED_RX.search(body) or DEFECT_RX.search(body):   # уже купил/использует, есть проблема — QR уместен
        return reply
    if _QR_RX.search(reply):
        reply = _QR_RX.sub("в нашем чате", reply)
        reply = re.sub(r"в\s+чат\w*\s+в\s+нашем\s+чате", "в нашем чате", reply, flags=re.I)
        reply = re.sub(r"\s{2,}", " ", reply).replace(" ,", ",").replace(" .", ".").strip()
    return reply


def _answer(client, r, cf, corpus):
    """Полный движок ответа на ОДИН элемент. → (out_dict, reply, route, conf, ground, used_llm, used_web)."""
    used_web = False
    if r["kind"] == "question":
        used_llm = True
    else:
        _txt = (r["body"] or "") + " " + (r["pros"] or "") + " " + (r["cons"] or "")
        _neg = (r["rating"] or 5) <= 3
        # LLM — только для положительных отзывов с вопросом/проблемой по сути; обычный позитив и
        # негатив → шаблоны (позитив: разнообразная ротация 16 вариантов; негатив: хендофф по QR)
        used_llm = _has_text(r) and (not _neg) and (bool(DEFECT_RX.search(_txt)) or "?" in _txt)
    if used_llm:
        try:
            d, cc = _llm(client, r, cf, corpus)
        except Exception as e:
            d, cc = {"reply": f"[ошибка вызова: {str(e)[:120]}]", "route": "review",
                     "confidence": 0, "grounded": False, "note": ""}, ""
        reply = (d.get("reply") or "").strip()
        route = "auto" if d.get("route") == "auto" else "review"
        conf = float(d.get("confidence") or 0)
        ground = {"llm": True, "grounded": bool(d.get("grounded")), "note": (d.get("note") or "")[:300],
                  "model": MODEL, "catalog": "КАТАЛОГ" in (cc or ""), "source": "карточка"}
        cat = "question" if r["kind"] == "question" else "review-text"
        # СОВМЕСТИМОСТЬ: карточка-семья (вариант серии) → прямой ответ; регуляторный код или модель
        # вне карточки → веб (источник №3, объяснит напр. L662B = европейское обозначение CX17NF)
        if r["kind"] == "question" and intent(r["body"]) == "совместимость модели":
            fct = cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else cf.for_wb(r["item_id"])
            code = (fct or {}).get("code")
            st, mm = _fam_status(r["body"], (fct or {}).get("models") or [])
            asked_m = _asked_models(r["body"])
            defect = re.search(r"вернуть|возврат|не\s+счита|не\s+вид|ошибк", (r["body"] or "").lower())
            reg = [x for x in re.findall(r"\b[A-Za-z]\d{3,4}[A-Za-z]\b", r["body"] or "")
                   if _norm(x) not in _norm(cc or "")]
            fam_reply = (f"Здравствуйте! Да, подойдёт для {', '.join(mm)} — это вариант серии из списка "
                         f"совместимости карточки." + (f" Наш картридж — {code}." if code else "")) if mm else ""
            # веб: регуляторный код ИЛИ модель есть, но карточка/серия её НЕ подтвердили (unknown/no_data)
            want_web = (not defect) and (bool(reg) or (st != "yes" and bool(asked_m)))
            if want_web:
                wa = web_compat(client, r["body"], r["product_name"], cc)
                used_web = True
                if wa and wa.get("verdict") in ("yes", "no") and (wa.get("reply") or "").strip():
                    reply = wa["reply"].strip()
                    ground.update({"web": True, "source": "веб", "grounded": True,
                                   "verdict": wa["verdict"], "sources": wa.get("sources", []),
                                   "note": "веб: " + (wa.get("note") or "")[:220]})
                elif st == "yes" and fam_reply:
                    reply = fam_reply
                    ground.update({"grounded": True, "source": "карточка-серия",
                                   "note": f"вариант серии: {', '.join(mm)}; веб-вердикт неясен"})
            elif st == "yes" and not defect and fam_reply:
                reply = fam_reply
                ground.update({"grounded": True, "source": "карточка-серия",
                               "note": f"вариант серии, совпало по базе: {', '.join(mm)}"})
            route = "review"
    else:
        name = _first_name(r["payload"]) if r["platform"] == "wb" else None
        _c, reply, route, conf = draft_review(r, name, _short(r["product_name"]))
        cc, ground = "", {"llm": False, "note": "шаблон отзыва (ротация вариантов)", "source": "шаблон",
                          "template": True}
        cat = "review-empty"
    reply = _presale_scrub(reply, r)
    outd = dict(r, cat=cat, reply=reply, route=route, conf=conf, card=cc,
                note=ground.get("note", ""), grounded=ground.get("grounded", False),
                catalog=ground.get("catalog", False), source=ground.get("source", ""),
                web=ground.get("web", False), sources=ground.get("sources", []),
                intent=intent(r["body"]) if r["kind"] == "question" else "")
    return outd, reply, route, conf, ground, used_llm, used_web


def run(since="2026-06-17"):
    rows = _gather(since)
    cf, corpus = CardFacts(), load_corpus()
    client = _client()
    print(f"Свежий необработанный поток с {since}: {len(rows)} (вопросов "
          f"{sum(r['kind']=='question' for r in rows)}, отзывов {sum(r['kind']=='review' for r in rows)}). "
          f"Корпус few-shot: {len(corpus.items)}.", flush=True)

    out, nllm, nweb = [], 0, 0
    for i, r in enumerate(rows, 1):
        outd, reply, route, conf, ground, ul, uw = _answer(client, r, cf, corpus)
        _store(r, reply, route, conf, ground)
        out.append(outd)
        nllm += 1 if ul else 0
        nweb += 1 if uw else 0
        if ul:
            tag = "ВОПРОС" if r["kind"] == "question" else f"ОТЗЫВ {r['rating']}★"
            print(f"[{i}/{len(rows)}] {r['platform']} · {tag} · {outd['intent']}", flush=True)
            print("   Q:", (r["body"] or r["pros"] or "")[:120].replace("\n", " "), flush=True)
            print("   →:", reply[:200].replace("\n", " "), f"[{route}]", flush=True)

    _html(out, since)
    c = Counter(o["cat"] for o in out)
    print(f"\nИТОГ: {len(out)} черновиков · ИИ-вызовов {nllm} · веб-проверок {nweb} · вопросов {c['question']} · "
          f"отзывов-с-текстом {c['review-text']} · пустых-шаблоном {c['review-empty']}", flush=True)
    print(f"Артефакт-файл: {ART}", flush=True)
    return out


def _e(s):
    return html.escape(str(s or ""))


def _html(out, since):
    qs = [o for o in out if o["cat"] == "question"]
    rt = [o for o in out if o["cat"] == "review-text"]
    re_ = [o for o in out if o["cat"] == "review-empty"]

    def q_row(o):
        card = _e(o["card"])[:900].replace("\n", "<br>") if o["card"] else '<span class="muted">карточка не сшита</span>'
        src = o.get("source") or ("карточка" if o["card"] else "")
        scls = {"веб": "s-web", "карточка-серия": "s-fam"}.get(src, "s-card")
        src_chip = f'<span class="chip {scls}">источник: {_e(o["catalog"] and "каталог+" or "")}{_e(src or "—")}</span>'
        links = ""
        if o.get("sources"):
            items = "".join(f'<li><a href="{_e(s.get("url"))}" target="_blank" rel="noopener">{_e(s.get("title") or s.get("url"))[:80]}</a></li>'
                            for s in o["sources"][:5])
            links = f'<div class="links"><span class="lbl">веб-источники</span><ul>{items}</ul></div>'
        return f"""<div class="item">
 <div class="ihead"><span class="plat">{_e(o['platform'])}</span>
   <span class="tag">{_e(o['intent'])}</span>{src_chip}
   <span class="badge b-review">на вычитку</span></div>
 <div class="q">{_e(o['body'])}</div>
 <div class="prod muted">{_e(o['product_name'])[:70]}</div>
 <div class="reply"><span class="lbl">черновик ответа</span>{_e(o['reply'])}</div>
 {links}
 <details class="src"><summary>факты, на которых построен ответ</summary>
   <div class="facts">{card}</div>
   <div class="note muted">grounded={str(o['grounded']).lower()} · {_e(o['note'])[:220]}</div></details>
</div>"""

    def r_row(o):
        return f"""<div class="item">
 <div class="ihead"><span class="plat">{_e(o['platform'])}</span>
   <span class="tag">отзыв {_e(o['rating'])}★</span>
   <span class="badge {'b-auto' if o['route']=='auto' else 'b-review'}">{'авто' if o['route']=='auto' else 'на вычитку'}</span></div>
 <div class="q">{_e((o['body'] or '') + (' · ' + o['pros'] if o['pros'] else '') + (' · ' + o['cons'] if o['cons'] else '')) or '(без текста)'}</div>
 <div class="prod muted">{_e(o['product_name'])[:70]}</div>
 <div class="reply"><span class="lbl">черновик ответа</span>{_e(o['reply'])}</div></div>"""

    style = """:root{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
@media(prefers-color-scheme:dark){:root{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}}
:root[data-theme="dark"]{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}
:root[data-theme="light"]{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
*{box-sizing:border-box}body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.wrap{max-width:900px;margin:0 auto;padding:26px 20px 48px}.eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}.sub{color:var(--muted);margin:0 0 18px;max-width:74ch}
.tiles{display:flex;gap:12px;margin:16px 0;flex-wrap:wrap}.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;flex:1;min-width:120px}
.tile .n{font-size:26px;font-weight:750;font-variant-numeric:tabular-nums}.tile .k{font-size:12px;color:var(--muted);margin-top:4px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.item{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin:12px 0}
.ihead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}.plat{font-weight:650;text-transform:capitalize}
.tag{font-size:11.5px;color:var(--muted)}.chip{font-size:11px;font-weight:650;color:var(--chipink);background:var(--chip);padding:2px 8px;border-radius:20px}
.chip.s-web{color:var(--auto);background:var(--auto-bg)}.chip.s-fam{color:var(--review);background:var(--review-bg)}
.links{margin-top:8px}.links ul{margin:4px 0 0;padding-left:18px}.links li{font-size:12px;margin:2px 0}.links a{color:var(--accent)}
.badge{margin-left:auto;display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750;white-space:nowrap}.b-auto{color:var(--auto);background:var(--auto-bg)}.b-review{color:var(--review);background:var(--review-bg)}
.q{font-weight:550;margin:2px 0}.prod{font-size:12px;margin:2px 0 10px}
.reply{background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;padding:10px 12px;font-size:14px}
.lbl{display:block;font-size:10.5px;font-weight:750;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);margin-bottom:4px}
.src{margin-top:9px}.src summary{cursor:pointer;font-size:12px;color:var(--muted)}.facts{font-size:12px;background:var(--surface2);border-radius:8px;padding:9px 11px;margin-top:7px;line-height:1.5}
.note{font-size:11.5px;margin-top:6px}.muted{color:var(--muted)}
.foot{color:var(--muted);font-size:13px;margin-top:24px;max-width:78ch;border-top:1px dashed var(--dash);padding-top:14px}"""
    body = f"""<div class="wrap"><p class="eyebrow">Цифровой квадрат · черновики на свежий поток</p>
<h1>Ответы-черновики: необработанные отзывы и вопросы</h1>
<p class="sub">Свежий необработанный поток с {_e(since)}. Вопросы и отзывы-с-текстом — ИИ-слой
(claude-sonnet-5) на фактах карточки (card_facts v2: WB-модели из описания, чип из Ozon-двойника)
+ каталог наших листингов + few-shot из наших прошлых ответов. Совместимость: сначала карточка
с учётом <b>вариантов серии</b> (CX17→CX17NF), затем <b>веб-поиск</b> для моделей, которых в карточке
нет. Пустые 5★ — шаблон. <b>Это черновики — на площадках ничего не опубликовано.</b></p>
<div class="tiles">
 <div class="tile"><div class="n">{len(qs)}</div><div class="k">вопросов (ИИ)</div></div>
 <div class="tile"><div class="n">{len(rt)}</div><div class="k">отзывов с текстом (ИИ)</div></div>
 <div class="tile"><div class="n">{len(re_)}</div><div class="k">пустых 5★ (шаблон)</div></div>
 <div class="tile"><div class="n">{len(out)}</div><div class="k">всего черновиков</div></div></div>
<h2>Вопросы — {len(qs)}</h2>{''.join(q_row(o) for o in qs)}
<h2>Отзывы с текстом — {len(rt)}</h2>{''.join(r_row(o) for o in rt) or '<p class="muted">нет</p>'}
<h2>Пустые отзывы (5★) — {len(re_)} · шаблон</h2>{''.join(r_row(o) for o in re_[:12])}
{'<p class="muted">…и ещё ' + str(len(re_)-12) + ' по тому же шаблону (ротация вариантов).</p>' if len(re_)>12 else ''}
<p class="foot">Вопросы в фазе «только черновики» помечены «на вычитку» — публикацию решает оператор.
Совместимость решается по слоям: <b>карточка-серия</b> (вариант линейки, напр. CX17NF при CX17 в списке —
бесплатно), затем <b>веб-поиск</b> (Claude web_search: определяет, входит ли принтер покупателя в серию,
которую наш картридж покрывает, со ссылками на источники); если ни один слой не подтверждает —
модель честно просит уточнить и не выдумывает (ложное «да, подойдёт» = возврат). Раскройте «факты,
на которых построен ответ», чтобы видеть grounding.</p></div>"""
    ART.write_text(f'<title>Черновики ответов на свежий поток — Цифровой квадрат</title>\n<style>{style}</style>\n{body}',
                   encoding="utf-8")


if __name__ == "__main__":
    since = "2026-06-17"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    run(since)

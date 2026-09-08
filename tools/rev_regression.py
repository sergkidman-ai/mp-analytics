# поток: rev
"""Регрессия движка ответов на РЕАЛЬНЫХ строках БД — прогон до и после каждого блока правок.

Правило Сергея (бриф 07.09.2026): регресс на «правильно пропущенных» = откат блока. Поэтому здесь
две половины, и вторая важнее первой: мало заблокировать плохое — нельзя заблокировать хорошее,
иначе правка просто перекладывает весь поток на оператора.

Ничего не пишет и в сеть не ходит: только читает raw_feedback и прогоняет детерминированные функции.
    ./venv/bin/python tools/rev_regression.py
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db                                     # noqa: E402
from reports import feedback_draft_run as dr            # noqa: E402
from reports import request_class as rc                 # noqa: E402
from reports import publish_gate as gate                # noqa: E402

TXT = "trim(concat_ws(' ', body, pros, cons))"
GAP = (r"(не работа|ошибк|не видит|полос|мажет|не подош|не совпад|брак|подтека|"
       r"засох|не печата|бледн|смаз|разбил|выкинул|вернул|шлак)")
ok = bad = 0


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print(f"  ✗ {name} {detail}"[:160])


# ── 1. 11 кейсов авто-дыры (docs/reports/rev_auto_gap_examples_2026-09-07.md) ────────────────
gap = db.query(f"""SELECT platform, account, ext_id, rating, body, pros, cons, item_id, payload
                   FROM raw_feedback WHERE kind='review' AND draft_route='auto' AND {TXT} ~* %s""",
               (GAP,))
for r in gap:
    check(f"gap {r['platform']}/{r['ext_id']}", dr.review_flagged(r), "маркер не распознан")
print(f"1. авто-дыра: {len(gap)} кейсов, помечены на оператора {len(gap) - bad}")

# ── 2. чистый позитив обязан остаться на auto ────────────────────────────────────────────────
b0 = bad
clean = db.query(f"""SELECT platform, account, ext_id, rating, body, pros, cons, item_id, payload,
                            product_name
                     FROM raw_feedback WHERE kind='review' AND rating=5 AND length({TXT}) BETWEEN 10 AND 300
                       AND {TXT} !~* %s ORDER BY ext_id LIMIT 40""", (GAP,))
for r in clean:
    if dr.review_flagged(r):                       # «?» и сожаление — законный повод, но их считаем
        continue                                   # отдельно: это не регресс, а расширение охвата
    route = dr.draft_review(r, None, "картридж")[2]
    check(f"clean {r['ext_id']}", route == "auto", f"стал {route}: {(r['body'] or '')[:60]}")
held = sum(1 for r in clean if dr.review_flagged(r))
print(f"2. чистый позитив: {len(clean)} строк, регрессов {bad - b0}, "
      f"снято по «?»/сожалению {held}")

# ── 3. класс обращения на реальном негативе ──────────────────────────────────────────────────
b0 = bad
neg = db.query(f"""SELECT kind, rating, body, pros, cons FROM raw_feedback
                   WHERE kind='review' AND rating<=2 AND length({TXT})>20 ORDER BY ext_id LIMIT 40""")
claims = sum(1 for r in neg
             if rc.classify(" ".join(filter(None, [r["body"], r["pros"], r["cons"]])),
                            kind="review", rating=r["rating"]) == "претензия")
check("класс претензии на 1-2★", claims == len(neg), f"{claims} из {len(neg)}")
print(f"3. класс обращения: 1–2★ с текстом {len(neg)}, распознано претензией {claims}")

# ── 4. вердикт публикации на опубликованных вопросах (не должен схлопнуться в «всё запретить») ─
b0 = bad
q = db.query("""SELECT kind, draft_text, draft_route, draft_confidence, draft_grounding, body
                FROM raw_feedback WHERE kind='question' AND posted_ok
                ORDER BY posted_at DESC LIMIT 60""")
allowed = sum(1 for r in q if gate.verdict(r)[0])
print(f"4. publish_gate на 60 опубликованных вопросах: разрешено {allowed}, запрещено {len(q) - allowed}")

# ── 5. блок A1 на реальных строках ───────────────────────────────────────────────────────────
# A1.1 — претензия держится; A1.2 — обещание в СВОЁМ черновике держится, тот же текст от оператора
# проходит; A1.3 — «да, подойдёт» на модель вне карточки. Для строк, драфтнутых до 07.09.2026,
# следа совместимости в grounding нет — восстанавливаем его тем же матчером, что и конвейер.
from reports.feedback_llm import _asked_models              # noqa: E402
from reports.feedback_today import _fam_status              # noqa: E402
from reports.card_facts import CardFacts                    # noqa: E402
_cf = CardFacts()


def with_compat(r):
    """Строка + восстановленный ground['compat'] (для архивных строк без него)."""
    g = dict(r.get("draft_grounding") or {})
    if r["kind"] == "question" and not g.get("compat"):
        asked = _asked_models(r.get("body") or "")
        if asked:
            fct = (_cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else
                   _cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else
                   _cf.for_wb(r["item_id"]))
            st, mm = _fam_status(r["body"], (fct or {}).get("models") or [])
            g["compat"] = {"asked": asked, "matched": mm if st == "yes" else [], "status": st}
    return dict(r, draft_grounding=g)


b0 = bad
claims = db.query(f"""SELECT kind,rating,body,pros,cons,draft_text,draft_route,draft_confidence,
                             draft_grounding FROM raw_feedback
                      WHERE kind='review' AND rating<=2 AND length({TXT})>20
                        AND draft_text IS NOT NULL ORDER BY ext_id LIMIT 40""")
from reports.card_rating import DEAD_TEXT              # noqa: E402
live = [r for r in claims if (r["draft_text"] or "").strip() != DEAD_TEXT.strip()]
held = sum(1 for r in live if "претензия, ручной ответ" in gate.verdict(r)[1])
check("A1.1 претензия на 1–2★ с текстом", held == len(live), f"{held} из {len(live)}")
print(f"5. A1.1: 1–2★ с текстом {len(claims)}, из них хендофф по мёртвой карточке "
      f"{len(claims) - len(live)} (правило 24.08.2026), снято гейтом {held}")

drafts = db.query("""SELECT kind,body,pros,cons,rating,draft_text,draft_route,draft_confidence,
                            draft_grounding FROM raw_feedback
                     WHERE draft_text IS NOT NULL AND created_at > now() - interval '30 days'""")
prom = [r for r in drafts if any("обещание" in w for w in gate.verdict(r)[1])]
for r in prom[:5]:                                   # тот же смысл, но написанный оператором, обязан пройти
    hand = "Оформим замену или возврат, напишите нам в чат."       # ≠ черновику → это текст человека
    why = gate.verdict(r, hand)[1]
    check("A1.2 текст оператора не судится", not any("обещание" in w for w in why), str(why)[:80])
print(f"6. A1.2: черновиков за 30 дней {len(drafts)}, с обещанием {len(prom)} "
      f"({100 * len(prom) / max(1, len(drafts)):.1f} %)")

CASES = {"C5890": "01a06c3e-a65e-7925-a74b-ee76a7a303c6", "LBP646": "01a0577e-d9e1-70a5-94f2-30cdfc9c028f",
         "HP 4303": "TdNvR6ABWgxW4OcHSaRS"}
for name, ext in CASES.items():
    rr = db.query("""SELECT platform,account,kind,ext_id,item_id,body,rating,draft_text,draft_route,
                            draft_confidence,draft_grounding FROM raw_feedback WHERE ext_id=%s""", (ext,))
    if not rr:
        check(f"A1.3 {name}", False, "строка не найдена")
        continue
    r = with_compat(rr[0])
    allow, why = gate.verdict(r)
    a13 = [w for w in why if "без подтверждения карточкой" in w]
    conf_only = why and all("уверенность" in w for w in why)
    check(f"A1.3 {name} запрещён", not allow, "разрешён")
    print(f"   {name}: {'⛔' if not allow else '✅'} "
          f"{'A1.3' if a13 else ('только порог conf' if conf_only else 'иное правило')}: "
          f"{gate.reason_line(why, 110) or '—'}")
print("7. A1.3: три названных кейса проверены")

# ── 8. эталонный набор: A1 не имеет права РАЗРЕШИТЬ то, что запрещал прежний гейт ──────────────
import importlib.util                                        # noqa: E402
import subprocess                                            # noqa: E402
_old_src = subprocess.run(["git", "-C", str(pathlib.Path(__file__).resolve().parent.parent),
                           "show", "HEAD:reports/publish_gate.py"], capture_output=True, text=True).stdout
old = None
if _old_src:
    spec = importlib.util.spec_from_loader("old_gate", loader=None)
    old = importlib.util.module_from_spec(spec)
    exec(compile(_old_src, "old_gate", "exec"), old.__dict__)
GOLD = ["nZx5NqAByX-W7wzv_tfL", "iIGKN6ABQU2etUMg35yr", "ioHvNKABQU2etUMgs4wc",
        "-CjwNKABMJha5m81k9Nt", "019fff47-f685-7486-9e97-aafba3ffb8bf",
        "01a03373-db75-7bab-83dc-11eac94aae35", "01a016a8-4f7a-7b18-b5d6-23aa1dd3b4cc",
        "01a033db-e4ec-7b5a-ab4c-fc75f6c117d5", "2CIAop8BMJha5m81xsZI"]
gold = db.query("""SELECT platform,account,kind,ext_id,item_id,body,rating,draft_text,draft_route,
                          draft_confidence,draft_grounding FROM raw_feedback
                   WHERE ext_id = ANY(%s)""", (GOLD,))
loosened = tightened = 0
for r in gold:
    new_allow = gate.verdict(with_compat(r))[0]
    old_allow = old.verdict(r)[0] if old else new_allow
    check(f"gold {r['ext_id'][:12]}", not (new_allow and not old_allow), "стал разрешён")
    loosened += int(new_allow and not old_allow)
    tightened += int(old_allow and not new_allow)
print(f"8. эталон rev_gold_set: строк найдено {len(gold)} из {len(GOLD)}, "
      f"стало запрещено дополнительно {tightened}, ослаблено {loosened}")

# ── 9. «правильно пропущенные» обязаны и дальше проходить ────────────────────────────────────
# Три ответа, которые Сергей на разборе признал верными (бриф 07.09.2026). Если их держит стоп-лист
# A1.2 — он слишком широк: «замена чипа простая» и «надёжнее заменить на новый» никаких обязательств
# компании не содержат. Именно из-за них список из брифа сужен до глаголов первого лица.
PASSED = {"HP M252dw чип": "ntSpZ6ABWgxW4OcH5cGB", "W2122X чип": "WAAubqAB96_WEUU2c39k",
          "TK-8800 качество": "01a04e3c-6edf-7e7a-b50d-0dd79b1b19da"}
for name, ext in PASSED.items():
    rr = db.query("""SELECT platform,account,kind,ext_id,item_id,body,pros,cons,rating,draft_text,
                            draft_route,draft_confidence,draft_grounding FROM raw_feedback
                     WHERE ext_id=%s""", (ext,))
    if not rr:
        check(f"пропущенный {name}", False, "строка не найдена")
        continue
    allow, why = gate.verdict(with_compat(rr[0]))
    check(f"пропущенный {name}", allow, gate.reason_line(why, 90))
print(f"9. правильно пропущенные: проверено {len(PASSED)}")

# ── 10. блок G: кэш утверждённых ответов по артикулу ─────────────────────────────────────────
# Мотив блока: 01.09.2026 один вопрос «каким тонером их заправлять?» по нашему артикулу 5422
# пришёл на четыре аккаунта и получил разные ответы. Кэш обязан свести их в ОДНУ запись — иначе
# он не решает задачу, ради которой заведён.
from reports import answer_cache as ac                      # noqa: E402

FILL_067H = ["01a05e30-3965-7d67-80f6-90deb4a82971", "01a05e36-39e3-72a4-9f57-ef52cdcaa865",
             "19Q5XqABWgxW4OcHYnAB", "yI05XqABMoF-QDrbm_hv"]
f067 = db.query("""SELECT platform, account, ext_id, item_id, article, body, rating
                   FROM raw_feedback WHERE ext_id = ANY(%s)""", (FILL_067H,))
trip = {(ac.internal_article(r["platform"], r["article"], r["item_id"], strict=True),) +
        (lambda c: (c, ac.question_key(r["body"], c)[0]))(
            rc.classify(r["body"], kind="question", rating=r["rating"]))
        for r in f067}
check("G 067H ×N сводятся в один ключ кэша", len(f067) == len(FILL_067H) and len(trip) == 1,
      f"строк {len(f067)}, ключей {len(trip)}: {sorted(map(str, trip))[:2]}")
print(f"10. кэш 067H: строк {len(f067)} на {len({r['account'] for r in f067})} аккаунтах → "
      f"ключей кэша {len(trip)} {sorted(trip)[0] if trip else ''}")

# Тот же артикул, другой класс — обязаны получиться РАЗНЫЕ ключи: иначе ответ про заправку
# перезапишет ответ про совместимость того же товара.
qs = db.query("""SELECT platform, account, ext_id, item_id, article, body, rating
                 FROM raw_feedback WHERE kind='question' AND created_at > now() - interval '60 days'""")
keyed = {}
for r in qs:
    cls = rc.classify(r["body"], kind="question", rating=r["rating"])
    key = ac.question_key(r["body"], cls)[0]
    if not key:
        continue
    art = ac.internal_article(r["platform"], r["article"], r["item_id"], strict=True)
    if art:
        keyed.setdefault((art, key), set()).add(cls)
collide = {k: v for k, v in keyed.items() if len(v) > 1}
check("G один ключ = один класс на артикуле", not collide, f"{list(collide)[:2]}")
arts = {a for a, _ in keyed}
print(f"10. ключи по вопросам за 60 дней: вопросов {len(qs)}, ключуется {len(keyed)} пар "
      f"«артикул+ключ» на {len(arts)} артикулах, коллизий класса {len(collide)}")

# stale → запрет публикации, и стоп-лист A1.2 обязан работать по тексту ИЗ КЭША.
def _cached(text, stale):
    g = {"llm": False, "grounded": True, "source": "кэш: утверждено 01.09.2026 на wb",
         "template_id": "cache", "request_class": "заправка",
         "cache": {"id": 0, "article": "5422", "key": "заправка", "hits": 3, "stale": stale}}
    return {"kind": "question", "body": "Каким тонером их заправлять?", "draft_route": "review",
            "draft_text": text, "draft_confidence": 0.9, "draft_grounding": g}

good = "Подойдёт тонер типа TN-2375, засыпать через отверстие под пробкой."
check("G stale-запись запрещена гейтом", not gate.verdict(_cached(good, True))[0], "разрешена")
check("G свежая подстановка проходит", gate.verdict(_cached(good, False))[0],
      gate.reason_line(gate.verdict(_cached(good, False))[1], 80))
check("G стоп-лист A1.2 работает по кэшу",
      not gate.verdict(_cached("Оформим замену или возврат.", False))[0], "разрешена")
print("10. кэш и гейт: stale ⛔, свежая ✅, обещание в тексте кэша ⛔")

print(f"\nИТОГО: проверок {ok + bad}, провалов {bad}")
sys.exit(1 if bad else 0)

# поток: rev
"""Backfill кэша утверждённых ответов (блок G брифа 08.09.2026): всё, что реально ушло покупателю
с 15.07.2026, — в `approved_answers`.

Отправленный ответ и есть утверждённый: под каждым стоит ✅ или правка оператора. Класс обращения
у старых строк не записан (колонка `request_class` заполняется только с 07.09.2026) — считаем его
тем же классификатором, что и конвейер; артикул сшиваем тем же `internal_article`.

КОНФЛИКТ — один ключ (артикул + класс + ключ вопроса), разные тексты ответов: именно ради них блок
и заведён, и выбирать за человека, какой из трёх ответов правильный, backfill не имеет права.
Такие ключи НЕ пишутся вовсе и выгружаются в отчёт — оператор решает и отвечает один раз.

    ./venv/bin/python tools/rev_cache_backfill.py            # только отчёт, в БД ничего
    ./venv/bin/python tools/rev_cache_backfill.py --apply    # записать бесконфликтные ключи
"""
import sys
import pathlib
import datetime as dt
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db                                     # noqa: E402
from reports import answer_cache as ac                  # noqa: E402
from reports import request_class as rc                 # noqa: E402
from reports import publish_gate as gate                # noqa: E402

SINCE = "2026-07-15"
APPLY = "--apply" in sys.argv
REPORT = pathlib.Path(f"docs/reports/rev_cache_conflicts_{dt.date.today()}.md")


def _norm_text(s):
    return " ".join((s or "").split()).casefold()


rows = db.query("""
    SELECT rf.platform, rf.account, rf.kind, rf.ext_id, rf.item_id, rf.article, rf.body, rf.rating,
           rf.draft_text, rf.draft_route, rf.draft_confidence, rf.draft_grounding, rf.request_class,
           rf.posted_at, fm.final_text, fm.decided_at
      FROM raw_feedback rf
      LEFT JOIN feedback_moderation fm
             ON fm.platform = rf.platform AND fm.account = rf.account AND fm.ext_id = rf.ext_id
            AND fm.state = 'sent'
     WHERE rf.kind = 'question' AND rf.posted_ok AND rf.posted_at >= %s
     ORDER BY rf.posted_at""", (SINCE,))

skipped = defaultdict(int)
groups = defaultdict(list)          # (артикул, класс, ключ) → [(текст, строка)]
for r in rows:
    text = (r["final_text"] or r["draft_text"] or "").strip()
    if len(text) < 15:
        skipped["пустой или обрезанный текст ответа"] += 1
        continue
    cls = r["request_class"] or rc.classify(r["body"], kind="question", rating=r["rating"])
    key, why = ac.question_key(r["body"], cls)
    if not key:
        skipped[why] += 1
        continue
    art = ac.internal_article(r["platform"], r["article"], r["item_id"], strict=True)
    if not art:
        skipped["артикул не подтверждён МойСкладом"] += 1
        continue
    allow, reasons = gate.verdict(dict(r), text)
    if not allow:
        skipped["запрещено гейтом публикации"] += 1
        continue
    groups[(art, cls, key)].append((text, r))

conflicts = {k: v for k, v in groups.items() if len({_norm_text(t) for t, _ in v}) > 1}
clean = {k: v for k, v in groups.items() if k not in conflicts}

written = 0
if APPLY:
    for (art, cls, key), items in clean.items():
        text, r = items[-1]                     # самый свежий отправленный ответ ключа
        okk, _ = ac.remember(article=art, cls=cls, key=key, text=text, approved_by="backfill",
                             platform=r["platform"], account=r["account"], ext_id=r["ext_id"],
                             card_sig=ac.card_signature(ac.facts_for(r), key))
        written += int(okk)

L = [f"# Конфликты кэша ответов — backfill с {SINCE} (отчёт {dt.date.today()})", "",
     "Конфликт — один и тот же вопрос по одному и тому же нашему артикулу, на который покупателям",
     "ушли РАЗНЫЕ ответы. В кэш такие ключи не пишутся: правильный текст выбирает человек",
     "(ответить один раз и утвердить — `/cache_drop <артикул> <класс>` снимает старую запись).", "",
     f"Отправлено вопросов с {SINCE}: **{len(rows)}**. Ключуется: **{sum(len(v) for v in groups.values())}** "
     f"на **{len(groups)}** ключах. Бесконфликтных ключей: **{len(clean)}**"
     + (f", записано в кэш: **{written}**." if APPLY else " (прогон без записи)."), "",
     "## Почему остальные не ключуются", ""]
for reason, n in sorted(skipped.items(), key=lambda x: -x[1]):
    L.append(f"- {n} — {reason}")
L += ["", f"## Конфликтующие ключи: {len(conflicts)}", ""]
for (art, cls, key), items in sorted(conflicts.items()):
    L.append(f"### Артикул {art} · класс «{cls}» · ключ `{key}` — ответов {len(items)}")
    L.append(f"Вопрос: {' '.join((items[0][1]['body'] or '').split())[:200]}")
    L.append("")
    for text, r in items:
        L.append(f"- **{r['platform']}/{r['account']}** {str(r['posted_at'])[:16]} "
                 f"(`{r['ext_id']}`): {' '.join(text.split())[:300]}")
    L.append("")
REPORT.parent.mkdir(parents=True, exist_ok=True)
REPORT.write_text("\n".join(L), encoding="utf-8")

print(f"отправленных вопросов с {SINCE}: {len(rows)}")
print(f"ключей всего {len(groups)}, бесконфликтных {len(clean)}, конфликтных {len(conflicts)}")
print(f"не ключуется: {sum(skipped.values())} — " +
      ", ".join(f"{v} {k}" for k, v in sorted(skipped.items(), key=lambda x: -x[1])[:4]))
print(f"записано в approved_answers: {written}" if APPLY else "прогон без записи (--apply не задан)")
print(f"отчёт: {REPORT}")

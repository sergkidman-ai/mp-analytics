# поток: rev
"""Машинный вердикт «можно ли публиковать черновик покупателю».

Работа №1 из аудита 25.08.2026 (`/opt/mp-knowledge/auto-inbox/rev-qa-audit-2026-08-25.md`).
До неё сигналы недоверия (`grounded`, `confidence`, `no_card`, `code_guard`, `qa_guard`) писались
в `raw_feedback.draft_grounding`, но их не читала НИ ОДНА развилка публикации: 94 % черновиков
с маркером неуверенности уходили покупателю по ✅ оператора, публиковались ответы с conf 0.0.

Два класса причин, и порядок между ними важен (замечание независимого ревью):

* ЖЁСТКИЕ — детерминированные, посчитаны кодом: маршрут `human`, маркер-заглушка `⚠️`, пустая
  карточка (`no_card`), сработавший `code_guard`/`qa_guard`, ответ без источника (`source='модель'`).
  Им верим: это факты конвейера.
* МЯГКИЕ — то, что о себе сообщила сама языковая модель (`grounded`, `confidence`). Им верим
  меньше, и строить на них вердикт в одиночку нельзя — но для запрета публикации их достаточно,
  запрет всегда в сторону «не отправлять».

Вердикт НЕ хранится в БД: он чистая функция от строки `raw_feedback` и пересчитывается на каждом
обращении. Отдельная колонка причины эскалации — работа №4, здесь не нужна.
"""
import json
import os

MIN_CONF = float(os.getenv("FEEDBACK_MIN_CONF", "0.6"))


def _ground(row):
    g = row.get("draft_grounding")
    if isinstance(g, str):
        try:
            g = json.loads(g)
        except Exception:
            g = None
    return g if isinstance(g, dict) else {}


def verdict(row, text=None):
    """→ (allow: bool, reasons: list[str]). Пустой список причин = публиковать можно.

    text — фактический текст к отправке; если он отличается от черновика, значит его писал человек,
    и судить машинными сигналами о черновике уже нечего (вызывающий обязан пометить это override).
    """
    reasons = []
    kind = row.get("kind")
    g = _ground(row)
    body = (text if text is not None else row.get("draft_text")) or ""

    # --- жёсткие: детерминированные факты конвейера ---
    if row.get("draft_route") == "human":
        reasons.append("маршрут human (домен-фильтр или битый JSON модели)")
    if body.strip().startswith("⚠️"):
        reasons.append("маркер-заглушка «на человека», это не ответ покупателю")
    if g.get("no_card"):
        reasons.append("нет данных карточки — ответ собран по каталогу/вебу")
    if g.get("code_guard"):
        reasons.append("code-guard вырезал код расходника, которого нет ни в карточке, ни в вопросе")
    if g.get("qa_guard"):
        qa = g.get("qa_guard")
        det = ", ".join(map(str, qa))[:120] if isinstance(qa, (list, tuple)) else str(qa)[:120]
        reasons.append(f"qa-guard: {det}")
    source = str(g.get("source") or "")
    if kind == "question" and source == "модель":
        reasons.append("источник — только модель: ни карточка, ни каталог, ни веб не подтвердили")
    if kind == "question" and source.startswith("карточка-серия"):
        # «вариант серии, совпало по базе» строит _fam_status() двусторонним совпадением подстроки
        # (feedback_today.py:198): «412» матчится и в «B412dn», и в «MB472». Пока дефект A1 не
        # исправлен токенайзером (работа №8), это не доказательство, а догадка.
        reasons.append("совместимость выведена совпадением подстроки в названии серии (дефект A1)")
    if "каталог-после-веба" in source:
        reasons.append("в ответ подставлен другой наш лот — проверьте, что текущий покупателю не подходит")

    # --- мягкие: то, что модель сообщила о себе. Только для ответов, которые она и писала ---
    if g.get("llm"):
        if kind == "question" and g.get("grounded") is False:
            reasons.append("модель сама не считает ответ обоснованным (grounded=false)")
        try:
            conf = float(row.get("draft_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < MIN_CONF:
            reasons.append(f"уверенность {conf:.2f} ниже порога {MIN_CONF:.2f}")

    return (not reasons), reasons


def reason_line(reasons, limit=300):
    """Одна строка причин для карточки оператора и лога отправки."""
    return "; ".join(reasons)[:limit]

# поток: rev
"""feedback_bot/purge_error_drafts.py — вычистить черновики, в которые попал ТЕКСТ ОШИБКИ модели.

Инцидент 03.08.2026: «[ошибка вызова: Error code: 529 … overloaded_error]» уехал в карточку
модерации как «Наш ответ» (вопрос про 62XL). Дыру закрыли в reports/llm_client.py (повторы +
LlmUnavailable, запись остаётся без черновика), этот скрипт убирает УЖЕ НАКОПЛЕННОЕ:

  1. raw_feedback: draft_text с текстом ошибки → все draft_*-поля обнуляются. draft_src_hash тоже,
     поэтому следующий цикл сгенерит ответ заново.
  2. feedback_moderation: если по такой записи уже ушла карточка в Telegram (tg_msg_id) — сообщение
     ЗАМЕНЯЕТСЯ через edit_text на пометку об отзыве (кнопки при этом снимаются), строка очереди
     возвращается в 'queued' с пустым tg_msg_id, чтобы после перегенерации ушла нормальная карточка.
     Уже опубликованные на площадке (state='sent') НЕ трогаем — их правит человек, отзыв невозможен.

Запуск:  ./venv/bin/python feedback_bot/purge_error_drafts.py [--apply]   (без --apply — только показ)
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                          # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                      # noqa: E402

# Узкий шаблон: только служебные маркеры сбоя. «529» отдельно НЕ ищем — это ещё и картридж Canon 529
# (10 живых черновиков), по нему нельзя чистить.
ERR_RX = r"(ошибка вызова|Error code|Overloaded|overloaded_error|web-error)"

REVOKED = ("⚠️ <b>Карточка отозвана.</b>\n\nВ черновик попал служебный текст ошибки модели "
           "(перегрузка API), а не ответ покупателю. Вопрос остался неотвеченным — ответ "
           "перегенерируется, придёт новая карточка.")


def find():
    return db.query(f"""SELECT f.platform, f.account, f.kind, f.ext_id, f.draft_route,
               left(f.draft_text, 80) AS t,
               m.id AS mid, m.state, m.tg_chat_id, m.tg_msg_id
          FROM raw_feedback f
          LEFT JOIN feedback_moderation m ON (m.platform,m.account,m.kind,m.ext_id)
                                          = (f.platform,f.account,f.kind,f.ext_id)
         WHERE f.draft_text ~* %s ORDER BY f.draft_at DESC""", (ERR_RX,))


def purge(rows, apply=False):
    from feedback_bot import tg_moderation as tm
    cleaned = revoked = 0
    for r in rows:
        key = (r["platform"], r["account"], r["kind"], r["ext_id"])
        if r["mid"] and r["state"] == "sent":
            print(f"  ПРОПУСК (уже опубликовано, отзыв невозможен): {key}")
            continue
        if apply:
            db.execute("""UPDATE raw_feedback SET draft_text=NULL, draft_route=NULL,
                draft_confidence=NULL, draft_category=NULL, draft_grounding=NULL,
                draft_at=NULL, draft_src_hash=NULL
                WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""", key)
        cleaned += 1
        if r["mid"]:
            if r["tg_msg_id"] and apply:
                tm.edit_text(r["tg_chat_id"], r["tg_msg_id"], REVOKED)   # без reply_markup = кнопки снимаются
            if apply:
                db.execute("""UPDATE feedback_moderation SET state='queued', tg_msg_id=NULL,
                    final_text=NULL, error=NULL WHERE id=%s""", (r["mid"],))
            revoked += 1
    return cleaned, revoked


def main(apply=False):
    rows = find()
    print(f"Черновиков с текстом ошибки: {len(rows)}")
    for r in rows:
        print(f"  {r['platform']}/{r['account']} {r['kind']} {r['ext_id']} "
              f"[{r['draft_route']}] карточка={r['state'] or '—'} msg={r['tg_msg_id'] or '—'}")
    if not rows:
        return 0, 0
    cleaned, revoked = purge(rows, apply=apply)
    print(f"{'ОЧИЩЕНО' if apply else 'БУДЕТ ОЧИЩЕНО'}: черновиков {cleaned}, карточек отозвано {revoked}")
    return cleaned, revoked


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)

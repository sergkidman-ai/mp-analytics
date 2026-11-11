# поток: rev
"""Owner-approved, fact-free replies to live 1/5-star reviews (11 November)."""
from reports.feedback_draft_run import review_flagged

POSITIVE = 'Здравствуйте! Спасибо за отзыв и оценку.'
NEGATIVE = ('Здравствуйте! Сожалеем, что покупка Вас разочаровала. Если вопрос ещё актуален, '
            'напишите нам в чат по QR-коду на упаковке или в товарном чеке.')
METHOD = 'owner_approved_simple_review_20261111'


def reply_for(row):
    if (row.get('kind') != 'review' or row.get('platform') not in ('wb', 'yandex')
            or row.get('skipped_old') or row.get('skipped_no_api')
            or row.get('final_text') is not None or row.get('decided_at') is not None
            or row.get('editing')):
        return None
    if row.get('rating') == 1:
        return NEGATIVE
    if row.get('rating') == 5 and not review_flagged(row):
        return POSITIVE
    return None


def matches(row, text=None):
    ground = row.get('draft_grounding') or {}
    expected = reply_for(row)
    return bool(expected and ground.get('verification_method') == METHOD
                and (text if text is not None else row.get('draft_text')) == expected)


def answer(row):
    reply = reply_for(row)
    if not reply:
        return None
    ground = {'source': 'Утверждённый владельцем шаблон', 'template_id': METHOD,
              'verification_method': METHOD, 'verify': 'not_required', 'grounded': True,
              'note': 'Простой автоответ без технических утверждений и вызовов LLM.'}
    return dict(row, reply=reply, route='auto'), reply, 'auto', 1.0, ground, False, False

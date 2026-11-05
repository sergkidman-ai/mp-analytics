# поток: rev
"""Archive questions must not incur Anthropic drafting or controller costs."""
from core import db


def is_archive_question(row):
    if (row or {}).get('platform')!='yandex' or row.get('kind')!='question':
        return False
    ground=row.get('draft_grounding') or {}
    if row.get('skipped_old') or (isinstance(ground,dict) and ground.get('historical_analysis')):
        return True
    if not row.get('ext_id'):
        return False
    return bool(db.query('''SELECT 1 FROM rev_history_analysis WHERE platform='yandex'
        AND account=%s AND kind='question' AND ext_id=%s''',(row.get('account') or 'ya_acc1',str(row['ext_id']))))


def require_paid_model_allowed(row):
    if is_archive_question(row):
        raise RuntimeError('Anthropic is disabled for archived Market questions by owner policy')

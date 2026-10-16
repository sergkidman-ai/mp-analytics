# поток: ev
"""Publish the selected rubrics of Ozon «Главное», without Telegram."""
import hashlib

from core import db
from ops import biz_diary

# Verified against the 24.08.2026 «Меры поддержки и страховые выплаты» notice
# in BRIEF_EV and identical publications in both accounts. chat_type=UNSPECIFIED
# also contains operational messages, so it must not be used as the selector.
CHANNELS = {
    "oz_acc1": "4ed864c0-74f9-47ae-ac17-edf7c26f68c8",
    "oz_acc2": "5c239fda-470a-48d3-a1cf-822c71ce72ab",
}
SELLER_API_TYPES = ("SELLER_API_UPDATES", "SELLER_API_NOTIFICATIONS")
EXTRA_CHANNELS = {
    'seller-api': {
        'oz_acc1': 'e306aa50-b7ea-4619-b87d-c1f1671c7614',
        'oz_acc2': '8d6104d3-446a-4a64-9b72-6d2977e4aa14',
    },
    'content': {
        'oz_acc1': '983a5dba-eb66-4cee-8720-cca24a601088',
        'oz_acc2': 'f785ba49-e358-407c-95f4-e4da68c7ff7e',
    },
    'fbs': {
        'oz_acc1': '37712812-778f-45d3-a2b8-76db90cd878c',
        'oz_acc2': '94d7b9a9-dbe3-4506-b3ad-4f53fc8014df',
    },
    'finance': {
        'oz_acc1': 'c2df270a-857f-4e3c-b4ec-9563eaeb9615',
        'oz_acc2': 'ace2f551-185d-49f9-8c9f-bebc566ed3cf',
    },
}
SECTION_LABELS = {
    'major': 'Крупные обновления', 'seller-api': 'Seller API',
    'content': 'Контент, качество и цены', 'fbs': 'FBS: новости о схеме',
    'finance': 'Финансы и документы',
}


def is_major(platform, account, chat_id):
    return platform == "ozon" and CHANNELS.get(account) == chat_id and chat_id is not None


def is_seller_api(platform, account, chat_type):
    return platform == "ozon" and account in CHANNELS and chat_type in SELLER_API_TYPES


def is_selected(platform, account, chat_id, chat_type=None):
    return section_for(platform, account, chat_id, chat_type) is not None


def section_for(platform, account, chat_id, chat_type=None):
    if is_major(platform, account, chat_id):
        return 'major'
    if is_seller_api(platform, account, chat_type):
        return 'seller-api'
    if platform == 'ozon' and account in CHANNELS and chat_id:
        return next((name for name, accounts in EXTRA_CHANNELS.items() if accounts.get(account) == chat_id), None)
    return None


def where_sql(section="all"):
    # All values are fixed source identifiers, not user input or credentials.
    accounts = " OR ".join(f"(account = '{account}' AND chat_id = '{chat}')" for account, chat in CHANNELS.items())
    major = f"(platform = 'ozon' AND ({accounts}))"
    seller_api = "(platform = 'ozon' AND account IN ('oz_acc1','oz_acc2') AND chat_type IN ('SELLER_API_UPDATES','SELLER_API_NOTIFICATIONS'))"
    sections = {'major': major}
    for name, channels in EXTRA_CHANNELS.items():
        pairs = ' OR '.join(f"(account='{account}' AND chat_id='{chat}')" for account, chat in channels.items())
        sections[name] = f"(platform='ozon' AND ({pairs}))"
    sections['seller-api'] = f"({seller_api} OR {sections['seller-api']})"
    if section in sections:
        return sections[section]
    if section != 'all':
        raise ValueError(section)
    return '(' + ' OR '.join(sections.values()) + ')'


def account_specific(row):
    section = section_for(row['platform'], row['account'], row['chat_id'], row.get('chat_type'))
    return section == 'finance' or (section == 'seller-api' and row.get('chat_type') != 'SELLER_API_UPDATES')


def publication_where(row):
    section = section_for(row['platform'], row['account'], row['chat_id'], row.get('chat_type'))
    scope = where_sql(section)
    chat_type = row.get('chat_type')
    if is_seller_api(row['platform'], row['account'], chat_type):
        scope += f" AND chat_type = '{chat_type}'"
    return scope + (f" AND account = '{row['account']}'" if account_specific(row) else '')


def publication_key(row):
    value = f"{row['d']}\n{row['title']}\n{(row['body'] or '').strip()}"
    section = section_for(row['platform'], row['account'], row['chat_id'], row.get('chat_type'))
    prefix = f"ozon:{section}:"
    if account_specific(row):
        prefix += row['account'] + ':'
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()


def publish(row, dry=False, planned=None):
    """Reuse exact source-linked/manual events; new identical cross-account notices share a key."""
    key = publication_key(row)
    scope = publication_where(row)
    section = section_for(row['platform'], row['account'], row['chat_id'], row.get('chat_type'))
    details = row['body'] or ''
    if section != 'major':
        details = SECTION_LABELS[section] + '\n\n' + details
    return publish_scoped(row, key, scope,
                          row['account'] if account_specific(row) else None,
                          details, dry=dry, planned=planned)


def publish_scoped(row, key, scope, account, details, *, expect=None, dry=False, planned=None):
    """Publish within a verified source scope, preserving existing events."""
    if dry and planned is not None and key in planned:
        return False
    existing = db.query(f"""
        SELECT id FROM biz_events
         WHERE dedup_key = %s
            OR id IN (SELECT event_id FROM mp_notices
                       WHERE {scope} AND title = %s AND body = %s
                         AND created_at::date = %s AND event_id IS NOT NULL)
            OR (dedup_key LIKE 'manual:ozon:%%' AND kind = 'mp' AND platform = 'ozon'
                AND (account IS NOT DISTINCT FROM %s OR %s IS NULL)
                AND event_date = %s AND title = %s)
         ORDER BY id LIMIT 1
    """, (key, row['title'], row['body'], row['d'], account, account, row['d'], row['title']))
    eid = existing[0]['id'] if existing else None
    is_new = eid is None
    if planned is not None:
        planned.add(key)
    if dry:
        return is_new
    if eid is None:
        eid = biz_diary.add(
            event_date=row['d'], kind='mp', platform='ozon',
            account=account, title=row['title'], details=details, expect=expect,
            source='ozon_chat', author='Ozon', dedup_key=key,
        )
        if eid is None:  # A concurrent collector inserted the same publication.
            eid = db.query("SELECT id FROM biz_events WHERE dedup_key=%s", (key,))[0]['id']
    # Link both exact copies together, including the counterpart that was not
    # selected after a previous pass. Existing manual notes are never rewritten.
    db.execute(f"""UPDATE mp_notices SET event_id=%s
                    WHERE {scope} AND title=%s AND body=%s
                      AND created_at::date=%s
                      AND (event_id IS NULL OR NOT EXISTS
                           (SELECT 1 FROM biz_events e WHERE e.id=mp_notices.event_id))""",
               (eid, row['title'], row['body'], row['d']))
    return is_new

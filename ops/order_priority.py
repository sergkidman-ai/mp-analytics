# поток: inv
"""Common dispatch order: express first, then original order time, oldest first."""
from datetime import timezone
from zoneinfo import ZoneInfo
from dateutil.parser import isoparse
from ops.fbs_ms_order_payload import EXPRESS_AGENT

MSK=ZoneInfo('Europe/Moscow')
EXPRESS_AGENTS=frozenset(EXPRESS_AGENT.values())


def contexts(document):
    if not isinstance(document,dict):return []
    result=[document]
    for field in ('market','market_order','mp_payload','payload'):
        child=document.get(field)
        if isinstance(child,dict):
            result.append(child)
            if isinstance(child.get('market_order'),dict):result.append(child['market_order'])
    return result


def is_express(document):
    for value in contexts(document):
        agent=value.get('agent') or {}
        if not isinstance(agent,dict):agent={}
        uid=value.get('agent_id') or agent.get('id') or agent.get('meta',{}).get('href','').rsplit('/',1)[-1]
        if uid in EXPRESS_AGENTS or value.get('is_express') is True or value.get('programType')=='EXPRESS':
            return True
    return False


def order_key(document, fallback=None):
    values=contexts(document)+contexts(fallback)
    stamp=float('inf')
    for field in ('moment','created_at','created','changed_at','first_seen_at'):
        found=False
        for value in values:
            raw=value.get(field)
            if raw is None:continue
            try:
                parsed=isoparse(raw) if isinstance(raw,str) else raw
                parsed=parsed.replace(tzinfo=MSK) if parsed.tzinfo is None else parsed
                stamp=parsed.astimezone(timezone.utc).timestamp();found=True;break
            except (TypeError,ValueError,AttributeError,OverflowError):continue
        if found:break
    identifier=next((str(v.get('name') or v.get('number') or v.get('posting_number') or v.get('id'))
                     for v in values if v.get('name') or v.get('number') or v.get('posting_number') or v.get('id')),'')
    return (0 if is_express(document) or is_express(fallback) else 1,stamp,identifier,str(document.get('id','')))

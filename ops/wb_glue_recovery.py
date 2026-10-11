# поток: mkt
"""Reconcile a grouping attempt before planning any new WB write."""
import json
import time

from ops import wb_bundle_regroup as regroup
from ops.wb_bundle_plan import fingerprint
from ops.wb_bundle_glue_singletons import content_fingerprint, now


class UnsafeRecovery(RuntimeError):
    """Live state cannot be explained by our recorded operation."""


class RecoveryNotReady(RuntimeError):
    """Read index is still changing; another delayed read may settle it."""


def entries(path, ident):
    if not path.exists():
        return []
    return [r for line in path.read_text().splitlines() if line.strip()
            for r in [json.loads(line)] if r.get('id') == ident]


def retryable(state, events):
    responses=[r for r in events if r.get('status')=='response']
    if responses:
        r=responses[-1];status=r.get('http',0)
        if status==429 or status>=500 or status==200:
            return True
        if status==400:
            try:data=json.loads(r.get('response',''))
            except ValueError:return False
            return data.get('error') is True and data.get('errorText','').strip() in {
                'Internal server error','target imt not found','Defunct IMT specified'}
        return False
    # Crash after a durable attempt but before recording its response is uncertain,
    # not permission to repeat. make_fresh proves before/after membership first.
    if any(r.get('status')=='attempt' for r in events):
        return True
    reason=str(state.get('reason','')).lower()
    return any(x in reason for x in ('timeout','timed out','connection','429','502','503','504',
                                     'read retries exhausted','anchor read missing'))


def read_complete(op, client):
    groups={}
    for expected in op['before_cards']:
        current=regroup.by_nm(client,expected['nmID'])
        root=current['imtID']
        if root not in groups:
            groups[root]=client.group(root)
    cards=[c for cc in groups.values() for c in cc]
    expected_ids={c['nmID'] for c in op['before_cards']}
    if len(cards)!=len(expected_ids) or {c['nmID'] for c in cards}!=expected_ids:
        raise UnsafeRecovery('Missing, duplicate or foreign cards in live groups')
    if content_fingerprint(cards)!=content_fingerprint(op['before_cards']):
        raise UnsafeRecovery('Card content changed; grouping recovery is forbidden')
    return cards


def mapping(cards):
    result={}
    for c in cards:
        result.setdefault(str(c['imtID']),set()).add(c['nmID'])
    return result


def explain_attempt(cards,op,events):
    """Accept only the recorded state before our request or its exact effect."""
    attempts=[r for r in events if r.get('status')=='attempt']
    live=mapping(cards)
    if not attempts:
        initial={str(k):set(v) for k,v in op['before_groups'].items() if v}
        if live!=initial:
            raise UnsafeRecovery('Membership changed without a recorded write')
        return 'unchanged_before_first_write'
    last=attempts[-1]
    before={str(k):set(v) for k,v in last['before_groups'].items()}
    affected=set().union(*before.values())
    current={root:ids & affected for root,ids in live.items() if ids & affected}
    if current=={k:v for k,v in before.items() if v}:
        return 'recorded_attempt_unchanged'
    moved=set(last['body']['nmIDs'])
    if not moved or not moved<=affected:
        raise UnsafeRecovery('Invalid last-attempt scope')
    expected={root:ids-moved for root,ids in before.items()}
    if 'targetIMT' in last['body']:
        root=str(last['body']['targetIMT'])
        expected.setdefault(root,set()).update(moved)
    else:
        roots={str(c['imtID']) for c in cards if c['nmID'] in moved}
        if len(roots)!=1 or roots & set(before):
            raise UnsafeRecovery('Uncertain detach does not have one new root')
        expected[next(iter(roots))]=moved
    if current!={k:v for k,v in expected.items() if v}:
        raise UnsafeRecovery('Live membership is neither the recorded before nor after state')
    return 'recorded_attempt_applied'


def make_fresh(op,client,events,pause=5):
    regroup.validate(op)
    first=read_complete(op,client)
    time.sleep(pause)
    second=read_complete(op,client)
    if fingerprint(first)!=fingerprint(second):
        raise RecoveryNotReady('Independent reads disagree; do not write')
    explanation=explain_attempt(second,op,events)
    current=mapping(second)
    for root in op.get('preferred_roots',{}):
        if str(root) not in current:
            raise UnsafeRecovery('Original preferred root disappeared')
    fresh={**op,'before_cards':second,'before_groups':{k:sorted(v) for k,v in current.items()},
           'before_fingerprint':fingerprint(second)}
    proof={'id':op['id'],'checked_at':now(),'explanation':explanation,
           'stable_groups':fresh['before_groups'],'content_unchanged':True,
           'last_attempt':next((r for r in reversed(events) if r.get('status')=='attempt'),None)}
    return fresh,proof

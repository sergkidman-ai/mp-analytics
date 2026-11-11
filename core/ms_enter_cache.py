# поток: infra
"""Complete MS Enter positions cached only under an explicitly verified document version.

Fresh document/version reads remain the caller's responsibility. No stock, reserve,
order composition, FIFO or source price estimates are inferred from this cache.
"""
from pathlib import Path
import gzip
import hashlib
import json
import os
import re
import uuid

ROOT = Path('/opt/mp-analytics/runtime/ms_enter_cache')


def _key(document):
    identity=document.get('id');version=document.get('updated')
    if not re.fullmatch(r'[0-9a-f-]{36}',str(identity or '')) or not isinstance(version,str) or not version:
        return None
    digest=hashlib.sha256(version.encode()).hexdigest()
    return identity,digest


def _digest(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def complete(page):
    if not isinstance(page,dict):return False
    rows=page.get('rows');size=page.get('meta',{}).get('size')
    if not isinstance(rows,list) or type(size) is not int or size!=len(rows):return False
    identities=[row.get('id') for row in rows if isinstance(row,dict)]
    return len(identities)==size and all(isinstance(identity,str) and identity for identity in identities) and len(set(identities))==size


def read(document):
    key=_key(document)
    if key is None:return None
    path=ROOT/key[0]/(key[1]+'.json.gz')
    try:
        with gzip.open(path,'rt',encoding='utf-8') as stream:data=json.load(stream)
        page=data['page']
        if (data.get('schema')!=1 or data.get('id')!=document['id']
            or data.get('updated')!=document['updated'] or not complete(page)
            or data.get('sha256')!=_digest(page['rows'])):return None
        return page
    except (OSError,ValueError,KeyError,TypeError,EOFError):return None


def write(document,page):
    """Caller has already verified completeness AND matching pre/post MS versions."""
    key=_key(document)
    if key is None or not complete(page):return False
    folder=ROOT/key[0]
    target=folder/(key[1]+'.json.gz');temp=folder/(key[1]+'.'+uuid.uuid4().hex+'.tmp')
    data=dict(schema=1,id=document['id'],updated=document['updated'],
              sha256=_digest(page['rows']),page={'meta':{'size':len(page['rows'])},'rows':page['rows']})
    try:
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        with gzip.open(temp,'wt',encoding='utf-8',compresslevel=1) as stream:json.dump(data,stream,ensure_ascii=False)
        os.chmod(temp,0o600);os.replace(temp,target)
        return True
    except OSError:
        try:temp.unlink(missing_ok=True)
        except OSError:pass
        return False

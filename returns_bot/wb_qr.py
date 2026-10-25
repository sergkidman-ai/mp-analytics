# поток: ret
"""Private current-day WB buyer QR cache; uploaded by the paired Windows extension."""
import base64
import hashlib
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from PIL import Image

MSK=timezone(timedelta(hours=3))
ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.private/wb-buyer-diskver/daily'
CACHE_DIGITAL=ROOT/'.private/wb-buyer-digital/daily'
ACCOUNTS={
    'wb_acc1':{'slug':'tsifrovoy','title':'Цифровой','org':'Цифровой'},
    'wb_acc2':{'slug':'diskver','title':'Дисквер','org':'Дисквэр'},
}
ACCOUNT_BY_SLUG={v['slug']:k for k,v in ACCOUNTS.items()}

def cache_for(account):
    if account=='wb_acc1':return CACHE_DIGITAL
    if account=='wb_acc2':return CACHE
    raise ValueError('unknown_account')

def enabled_accounts():
    # Existing Diskver remains enabled; a new company joins reports after its first real QR.
    return tuple(a for a in ACCOUNTS if a=='wb_acc2' or (cache_for(a)/'.paired').exists())
MONTHS=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря']

def now_msk(): return datetime.now(MSK)

# One explicitly requested full-chain test. Other Sundays stay disabled.
ONE_OFF_TEST_DATE='2026-10-25'
def enabled_day(now):
    return now.weekday()!=6 or now.date().isoformat()==ONE_OFF_TEST_DATE

def valid_today(text,now):
    m=re.search(r'\b(\d{1,2})\s+('+'|'.join(MONTHS)+r')\b',text or '',re.I)
    return bool(m and int(m[1])==now.day and MONTHS.index(m[2].lower())+1==now.month
                and re.search(r'до\s+00:00',text or '') and re.search(r'мск|москв',text or '',re.I))

def decode_png(data):
    import zxingcpp
    if not data.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('not_png')
    with Image.open(io.BytesIO(data)) as im:
        if im.width*im.height>8_000_000 or min(im.size)<150:raise ValueError('image_size')
        results=zxingcpp.read_barcodes(im)
    if len(results)!=1 or results[0].format!=zxingcpp.BarcodeFormat.QRCode:raise ValueError('qr_not_unique')
    return hashlib.sha256(results[0].bytes).hexdigest()

def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.tmp-')
    try:
        with os.fdopen(fd,'wb') as f:f.write(data)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def store(png,day,validity,now=None,*,account="wb_acc2"):
    now=now or now_msk()
    if not enabled_day(now):raise ValueError('sunday_disabled')
    if day!=now.date().isoformat() or not valid_today(validity,now):raise ValueError('not_today')
    payload_hash=decode_png(png)
    cache=cache_for(account)
    meta={'account':account,'day':day,'valid_until':datetime.combine(now.date()+timedelta(days=1),datetime.min.time(),MSK).isoformat(),
          'received_at':now.isoformat(),'sha256':hashlib.sha256(png).hexdigest(),'qr_sha256':payload_hash}
    # Readers fail closed while an update is between these two atomic writes.
    atomic(cache/(day+'.png'),png)
    atomic(cache/(day+'.json'),json.dumps(meta).encode())
    atomic(cache/'.paired',account.encode())
    return meta

def current(now=None,*,account="wb_acc2"):
    now=now or now_msk()
    if not enabled_day(now):return None
    key=now.date().isoformat();cache=cache_for(account)
    try:
        meta=json.loads((cache/(key+'.json')).read_text());data=(cache/(key+'.png')).read_bytes()
        if meta['account']!=account or meta['day']!=key or hashlib.sha256(data).hexdigest()!=meta['sha256']:return None
        if datetime.fromisoformat(meta['valid_until'])<=now:return None
        return data,meta
    except (OSError,ValueError,KeyError):return None

def attachment(now=None,*,account="wb_acc2"):
    result=current(now,account=account)
    if not result:return None
    data,meta=result
    until=datetime.fromisoformat(meta['valid_until']).astimezone(MSK)
    info=ACCOUNTS[account]
    return ('wb-qr-'+info['slug']+'-'+meta['day']+'.png',data,
            'Wildberries - '+info['title']+'\n'
            +f'Действует до {until:%d.%m %H:%M} МСК.', 'image/png')

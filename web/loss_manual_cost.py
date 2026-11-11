# поток: fin
"""Explicit manual loss valuations, audited and independent from automatic MS cost."""
from copy import deepcopy
from decimal import Decimal
import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, condecimal
from psycopg2.extras import RealDictCursor, Json
from core import db

router=APIRouter()


class ManualKey(BaseModel):
    snapshot_id: str = Field(min_length=1,max_length=100)
    account: Literal['wb_acc1','wb_acc2']
    warehouse: str = Field(min_length=1,max_length=200)
    nm_id: int = Field(gt=0)
    version: int = Field(ge=0)
    expected_missing_qty: int = Field(ge=0)


class ManualCost(ManualKey):
    unit_cost: condecimal(gt=0,max_digits=12,decimal_places=2)
    note: str = Field(default='',max_length=1000)


def apply_manual(payload,snapshot_id):
    out=deepcopy(payload)
    entries={(r['account'],r['warehouse'],r['nm_id']):r for r in db.query(
        'SELECT * FROM wb_loss_manual_cost WHERE snapshot_id=%s',(snapshot_id,))}
    out['manual_scope']=snapshot_id
    for account,data in out['accounts'].items():
        for i in data['items']:
            m=entries.get((account,i['warehouse'],i['nm_id']))
            missing=max(i['qty']-i['costed_qty'],0)
            q=min(m['qty'],missing) if m and m['enabled'] else 0
            amount=(m['unit_cost']*q).quantize(Decimal('.01')) if q else Decimal(0)
            i['manual']={k:str(v) if isinstance(v,Decimal) else v for k,v in m.items()} if m else None
            i['manual_costed_qty']=q;i['manual_cost_rub']=str(amount)
            i['total_costed_qty']=i['costed_qty']+q
            i['total_cost_rub']=str(Decimal(i['known_cost_rub'])+amount)
            i['remaining_qty']=missing-q
        s=data['summary'];s['manual_costed_qty']=sum(i['manual_costed_qty'] for i in data['items'])
        s['manual_cost_rub']=str(sum((Decimal(i['manual_cost_rub']) for i in data['items']),Decimal(0)))
        s['total_costed_qty']=s['costed_qty']+s['manual_costed_qty']
        s['total_cost_rub']=str(Decimal(s['known_cost_rub'])+Decimal(s['manual_cost_rub']))
        s['remaining_qty']=s['qty']-s['total_costed_qty']
    return out


def change_manual(body,request,clear=False):
    origin=request.headers.get('origin')
    if origin and urlsplit(origin).netloc!=request.headers.get('host'):
        raise HTTPException(403,'Изменение доступно только со страницы Пульта')
    actor=(request.headers.get('X-Pult-User') or 'local')[:200]
    key=(body.snapshot_id,body.account,body.warehouse,body.nm_id)
    # Snapshot lock serializes manual edits and excludes concurrent snapshot replacement.
    with db.get_conn() as conn,conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT payload FROM wb_loss_recovery_snapshot WHERE id=%s FOR UPDATE',(body.snapshot_id,))
        snap=cur.fetchone()
        if not snap or snap['payload'].get('mode')!='recovered_snapshot':
            raise HTTPException(404,'Снимок не найден')
        data=snap['payload'].get('accounts',{}).get(body.account,{})
        item=next((i for i in data.get('items',[]) if i['warehouse']==body.warehouse and i['nm_id']==body.nm_id),None)
        if not item:raise HTTPException(404,'Позиция не найдена в этой организации и на этом складе')
        cur.execute('SELECT * FROM wb_loss_manual_cost WHERE snapshot_id=%s AND account=%s AND warehouse=%s AND nm_id=%s FOR UPDATE',key)
        old=cur.fetchone()
        if (old['version'] if old else 0)!=body.version:
            raise HTTPException(409,'Сумму уже изменили. Обновите страницу и проверьте значение')
        missing=max(item['qty']-item['costed_qty'],0)
        if clear:
            if not old or not old['enabled']:raise HTTPException(409,'Ручная стоимость уже удалена')
            cur.execute('UPDATE wb_loss_manual_cost SET enabled=false,version=version+1,actor=%s,updated_at=now() WHERE snapshot_id=%s AND account=%s AND warehouse=%s AND nm_id=%s RETURNING *',(actor,*key))
        else:
            if not missing or missing!=body.expected_missing_qty:
                raise HTTPException(409,'Количество без себестоимости изменилось. Обновите страницу')
            cur.execute('''INSERT INTO wb_loss_manual_cost(snapshot_id,account,warehouse,nm_id,unit_cost,qty,note,actor)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(snapshot_id,account,warehouse,nm_id) DO UPDATE SET
              unit_cost=excluded.unit_cost,qty=excluded.qty,note=excluded.note,actor=excluded.actor,
              enabled=true,version=wb_loss_manual_cost.version+1,updated_at=now() RETURNING *''',
              (*key,body.unit_cost,missing,body.note.strip(),actor))
        new=cur.fetchone()
        encode=lambda value:Json(value,dumps=lambda x:json.dumps(x,default=str))
        cur.execute('''INSERT INTO wb_loss_manual_cost_history(snapshot_id,account,warehouse,nm_id,action,old_value,new_value,actor)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',(*key,'clear' if clear else 'save',encode(dict(old)) if old else None,encode(dict(new)),actor))
    return {'ok':True,'version':new['version']}


@router.post('/api/warehouse/loss/manual-cost')
def save_manual(body:ManualCost,request:Request):
    return change_manual(body,request)


@router.delete('/api/warehouse/loss/manual-cost')
def clear_manual(body:ManualKey,request:Request):
    return change_manual(body,request,clear=True)

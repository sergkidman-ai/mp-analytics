# поток: prc
"""Remove foreign goods from local work queues; preserve source rows and suggestions.
Never writes MS, marketplace data or the global supplier-article blacklist.
"""
from collections import Counter
import json
from psycopg2.extras import RealDictCursor,Json
from core import db
from .assortment import name_reason,VERSION


def reconcile():
    result={}
    encode=lambda value:Json(value,dumps=lambda x:json.dumps(x,ensure_ascii=False,default=str))
    with db.get_conn() as connection,connection.cursor(cursor_factory=RealDictCursor) as cursor:
        for kind,table,key,child,foreign in [
            ('novelty','prc_novelty','id','prc_novelty_candidate','novelty_id'),
            ('unlinked','prc_unlinked','ms_id','prc_unlinked_candidate','ms_id')]:
            cursor.execute(f'SELECT * FROM {table} FOR UPDATE')
            rows=cursor.fetchall();removed=0;reasons=Counter();suppliers=Counter()
            for row in rows:
                reason=name_reason(row['name'])
                if reason is None:continue
                cursor.execute(f'SELECT * FROM {child} WHERE {foreign}=%s',(row[key],));candidates=cursor.fetchall()
                cursor.execute('''INSERT INTO prc_assortment_excluded
                 (entity_type,entity_id,supplier_key,article,name,reason,rules_version,row_data,candidates)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                 (kind,str(row[key]),row['supplier_key'],row.get('article'),row['name'],reason,VERSION,encode(dict(row)),encode([dict(x) for x in candidates])))
                cursor.execute(f'DELETE FROM {table} WHERE {key}=%s',(row[key],))
                removed+=cursor.rowcount;reasons[reason]+=1;suppliers[row['supplier_key']]+=1
            result[kind]={'removed':removed,'by_reason':dict(reasons),'by_supplier':dict(suppliers)}
    return result

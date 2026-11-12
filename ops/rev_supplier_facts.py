# поток: rev
"""Read deployed main purchase policy, emit technical projections only."""
import json
from pathlib import Path
import sys

REV_ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,'/opt/mp-analytics')
from core import db
from core.supplier_quotes import load_book
from prices.set_overrides import COMPOSITIONS
from collections import Counter
import importlib.util

spec=importlib.util.spec_from_file_location('rev_projection',REV_ROOT/'reports/rev_fact_sources.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


def main():
    codes=json.load(sys.stdin)
    if not isinstance(codes,list) or len(codes)>200:raise ValueError('Bounded code list required')
    book=load_book();output={}
    for code in codes:
        quote=(book.assembled(Counter(COMPOSITIONS[code]),round_units=True) if code in COMPOSITIONS else book.ordinary(code))
        items=[]
        if quote:
            for offer,qty in quote.selected:
                rows=db.query('SELECT name FROM ms_product WHERE ms_id=%s',(offer.id,))
                if rows:items.append({'record':str(offer.id),'units':qty,'facts':module.technical_name(rows[0]['name'])})
        output[code]={'selected':bool(items),'items':items,'policy':'main/core/supplier_quotes'}
    print(json.dumps(output,ensure_ascii=False))


if __name__=='__main__':main()

# поток: rev
"""Incremental technical source refresh, isolated from answer generation/publication."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from core import db
from reports import rev_fact_sources as sources,question_evidence
from reports.card_facts import CardFacts


def refresh(max_pages=40):
    sources.setup()
    report={'catalog':sources.refresh_tk(max_pages=max_pages)}
    rows=db.query("""SELECT * FROM raw_feedback WHERE kind='question' AND NOT is_answered
        AND NOT coalesce(posted_ok,false) AND coalesce(recovery_active_unanswered,true)""")
    codes=set();facts=CardFacts();errors=0
    for row in rows:
        try:
            identity=sources.resolve(row,question_evidence.native(row,facts))
            if identity.get('status')=='matched':codes.add(identity['external_code'])
        except Exception:errors+=1
    try:report['choices']=sources.refresh_choices(codes) if codes else 0
    except Exception as error:report['choice_error']=type(error).__name__
    report['identity_errors']=errors
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--max-pages',type=int,default=40)
    args=parser.parse_args();print(json.dumps(refresh(args.max_pages),ensure_ascii=False))

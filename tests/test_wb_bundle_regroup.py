"""Safety checks for regrouping, independent of WB and production data."""
import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path
import ops.wb_bundle_regroup as module
from ops.wb_bundle_regroup import UNKNOWN, chip_info, colored_card, prepare, reasons, validate
from ops.wb_bundle_plan import fingerprint


def card(nm):
    return {'nmID':nm,'imtID':10,'vendorCode':f'{nm:04d}', 'subjectID':1820,
            'title':'Test','characteristics':[],'description':''}


def operation(parts):
    cc=[card(1),card(2),card(3)]
    return {'before_cards':cc,'before_fingerprint':fingerprint(cc),'partitions':parts}


def test_unknown_not_absent():
    assert chip_info(card(1),[],{},[])[0] == UNKNOWN


def test_explicit_conflict_cannot_be_inherited():
    assert chip_info(card(1),[],{'chip':'chip'},['без чипа'])[0]=='противоречие'


def test_no_loss_no_duplication_in_repartition():
    validate(operation([[1,2],[3]]))
    for parts in ([[1],[2]], [[1,2],[2,3]], [[1,2,3,4]]):
        with unittest.TestCase().assertRaises(RuntimeError): validate(operation(parts))


def test_limit_subject_and_content_integrity():
    op=operation([[1,2],[3]]);op['before_cards'][0]['title']='Changed'
    with unittest.TestCase().assertRaises(RuntimeError): validate(op)
    op=operation([[1,2],[3]]);op['before_cards'][0]['subjectID']=1
    op['before_fingerprint']=fingerprint(op['before_cards'])
    with unittest.TestCase().assertRaises(RuntimeError): validate(op)
    cc=[card(n) for n in range(31)]
    with unittest.TestCase().assertRaises(RuntimeError):
        validate({'before_cards':cc,'before_fingerprint':fingerprint(cc),'partitions':[list(range(31))]})


def test_resource_families_are_split_losslessly():
    cards=[];refs={}
    for i, resource in enumerate([1000,2000],1):
        code=f'{i:04d}';refs[code]={'title':f'M{i}','consumable_type':'Картридж','raw':{}}
        for q in (1,2,6,10):
            c=card(i*100+q);c['vendorCode']=code if q==1 else code+'X'+str(q)
            c['title']='Cartridge' if q==1 else f'Cartridge {q} шт'
            c['characteristics']=[{'id':5023,'value':[f'M{i}']},{'id':179792,'value':q},
              {'id':58854,'value':['черный']},{'id':88963,'value':resource},{'id':179078,'value':['Printer 100']}]
            cards.append(c)
    p=prepare({'cards':cards,'read_at':'test'},{'references':refs,'marks':{}})
    assert len(p['operations'])==1 and not p['review']
    assert sorted(map(len,p['operations'][0]['partitions']))==[4,4]
    validate(p['operations'][0])


def test_unknown_next_to_known_is_manual_review():
    c1,c2=card(1),card(2)
    for c in (c1,c2): c['characteristics']=[{'id':5023,'value':[c['vendorCode']]}]
    b={**c1,'nmID':3,'imtID':11,'vendorCode':'0001X2','title':'Test 2 шт',
       'characteristics':[{'id':5023,'value':['0001']},{'id':179792,'value':2}]}
    source={'references':{'0001':{'title':'0001','consumable_type':'Картридж'},
                          '0002':{'title':'0002','consumable_type':'Картридж','chip':'chip'}},'marks':{}}
    p=prepare({'cards':[c1,c2,b],'read_at':'test'},source)
    assert not p['operations']
    assert any('Неизвестный чип рядом с известным' in r['reason'] for r in p['review'])


def test_ink_same_code_can_use_tc_printer_source_when_wb_empty():
    p=card(1);p['characteristics']=[{'id':5023,'value':['T6641 K']},{'id':179792,'value':1}]
    b={**p,'nmID':2,'vendorCode':'0001X2','title':'Ink 2 шт',
       'characteristics':[{'id':5023,'value':['T6641 K']},{'id':179792,'value':2}]}
    ref={'title':'T6641 K','consumable_type':'Чернила','raw':{'printer_models':[{'brand':'Epson','title':'L100'}]}}
    assert not reasons(p,[b],ref,UNKNOWN)
    ref['raw']={}
    assert 'Не заполнена совместимость родителя' in reasons(p,[b],ref,UNKNOWN)


def test_single_ribbon_keeps_its_own_bundles():
    cc=[]
    for q in (1,2,6,10):
        c=card(q);c['vendorCode']='0001' if q==1 else '0001X'+str(q)
        c['title']='Ribbon' if q==1 else f'Ribbon {q} шт'
        c['characteristics']=[{'id':5023,'value':['Ribbon']},{'id':179792,'value':q},
                             {'id':179078,'value':['Printer']},{'id':58854,'value':['черный']}]
        cc.append(c)
    source={'references':{'0001':{'title':'Ribbon','consumable_type':'Лента для принтера','raw':{}}},'marks':{}}
    p=prepare({'cards':cc,'read_at':'test'},source)
    assert not p['operations'] and not p['review']


def test_combined_ink_colors_remain_a_color_group():
    c=card(1)
    c['characteristics']=[{'id':58854,'value':['голубой','желтый','пурпурный']}]
    assert colored_card(c)
    c['characteristics']=[{'id':58854,'value':['черный']}]
    assert not colored_card(c)
    cc=[]
    colors=['черный','голубой;желтый;пурпурный']
    for i,co in enumerate(colors,1):
        x=card(i);x['characteristics']=[{'id':5023,'value':[f'M{i}']},{'id':179792,'value':1},
                                       {'id':179078,'value':['Printer']},{'id':58854,'value':[co]}]
        cc.append(x)
    for q in (2,6,10):
        x={**cc[0],'nmID':100+q,'vendorCode':'0001X'+str(q),'title':f'M1 {q} шт'}
        x['characteristics']=[{'id':5023,'value':['M1']},{'id':179792,'value':q},
                              {'id':179078,'value':['Printer']},{'id':58854,'value':['черный']}]
        cc.append(x)
    src={'references':{f'{i:04d}':{'title':f'M{i}','consumable_type':'Картридж','raw':{}} for i in (1,2)},'marks':{}}
    p=prepare({'cards':cc,'read_at':'test'},src)
    assert len(p['operations'])==1 and not p['review']
    assert {frozenset(x) for x in p['operations'][0]['partitions']}=={frozenset([1,2,102]),frozenset([106,110])}


def test_only_an_explicit_rejection_permits_rechecked_attempt():
    class Response:
        def __init__(self,status,data):self.status_code=status;self.data=data
        def json(self):return self.data
    assert module.rejected_missing_target(Response(400,{'error':True,'errorText':'target imt not found'}))
    assert not module.rejected_missing_target(Response(500,{'error':True,'errorText':'target imt not found'}))
    assert not module.rejected_missing_target(Response(400,{'error':True,'errorText':'other error'}))


def test_rejected_move_is_rechecked_before_a_new_attempt():
    cc=[card(1),{**card(2),'imtID':11}];live={c['nmID']:dict(c) for c in cc};reads=[]
    class Response:
        content=b'{}';text='{}'
        def __init__(self,status):self.status_code=status
        def json(self):return {'error':True,'errorText':'target imt not found'} if self.status_code==400 else {}
        def raise_for_status(self):assert self.status_code==200
    class Client:
        writes=0
        def group(self,root):
            reads.append((self.writes,root))
            return [dict(c) for c in live.values() if c['imtID']==root]
        def move(self,body):
            self.writes+=1
            if self.writes==1:return Response(400)
            assert any(n==1 for n,r in reads)
            live[1]['imtID']=11
            return Response(200)
    client=Client()
    with patch.object(module,'by_nm',lambda cli,nm:dict(live[nm])),patch.object(module,'journal'),patch.object(module.time,'sleep'):
        module.perform(client,{'id':1},{'targetIMT':11,'nmIDs':[1]},live)
    assert client.writes==2 and live[1]['imtID']==11


def test_stale_detach_response_is_not_a_confirmation():
    original=[card(1),card(2)]
    actual=[{**original[0],'imtID':11},original[1]]
    class Response:
        status_code=200;content=b'{}';text='{}'
        def raise_for_status(self): pass
        def json(self): return {}
    class Client:
        def __init__(self): self.written=False
        def group(self,root): return [c for c in (actual if self.written else original) if c['imtID']==root]
        def move(self,body): self.written=True;return Response()
    state={c['nmID']:c for c in original};reads=[]
    def lookup(client,nm):
        reads.append(nm)
        return original[0] if len(reads)==1 else actual[0]
    with patch.object(module,'by_nm',lookup),patch.object(module,'journal'),patch.object(module.time,'sleep'):
        module.perform(Client(),{'id':1},{'nmIDs':[1]},state)
    assert len(reads)==2 and state[1]['imtID']==11


def test_repartition_retains_parent_group_root():
    op=operation([[1,2],[3]])
    op.update(id=10,anchor=1,rules=['test'],before_groups={'10':[1,2,3]})
    live={c['nmID']:dict(c) for c in op['before_cards']};bodies=[]
    class Client:
        def group(self,root): return [dict(c) for c in live.values() if c['imtID']==root]
    def perform(client,operation,body,state):
        bodies.append(body)
        for nm in body['nmIDs']:
            live[nm]['imtID']=body.get('targetIMT',11)
            state[nm]=dict(live[nm])
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp)/'docs/reports').mkdir(parents=True)
        with patch.object(module,'perform',perform),patch.object(module,'journal'),patch.object(module,'ROOT',Path(tmp)):
            result=module.execute(op,Client())
    assert result['status']=='confirmed'
    assert live[1]['imtID']==live[2]['imtID']==10 and live[3]['imtID']==11
    assert bodies==[{'nmIDs':[3]}]


if __name__=='__main__':
    for name,func in list(globals().items()):
        if name.startswith('test_'):
            func()
            print(name, 'OK')

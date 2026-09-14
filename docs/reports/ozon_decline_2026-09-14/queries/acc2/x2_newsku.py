# Разбор +5,8 тыс. SKU acc2 в ozon_price_index с 09.09 (read-only, из кэша)
import pandas as pd, re, numpy as np
C = 'cache/'; DC = '../decline/cache/'
out = []
def p(*a): out.append(' '.join(str(x) for x in a))
pi = pd.read_pickle(C + 'pidx2.pkl'); pi['d'] = pd.to_datetime(pi.collected_on)
P = pd.read_pickle(C + 'product2.pkl'); P2 = P[P.account == 'oz_acc2']; P1 = P[P.account == 'oz_acc1']
first = pi.groupby('offer_id').d.min(); last = pi.groupby('offer_id').d.max()
new = set(first[first >= '2026-09-09'].index); old = set(first[first < '2026-09-09'].index)
p('offer_id в индексе: всего', len(first), 'старые', len(old), 'новые с 09.09', len(new))
p('новые по дню первого появления', first[first >= '2026-09-09'].dt.strftime('%m-%d').value_counts().sort_index().to_dict())
pp = P2.drop_duplicates('offer_id').set_index('offer_id')
def pat(o): return re.sub(r'[0-9]', '9', re.sub(r'[A-Za-z]', 'A', o))
def core(o):
    m = re.match(r'^(\d{4})(X\d{1,2})?', o); return m.group(0) if m else None
N = pd.DataFrame({'offer_id': sorted(new)}); O = pd.DataFrame({'offer_id': sorted(old)})
for df in (N, O):
    df['in_prod'] = df.offer_id.isin(pp.index)
    df['archived'] = df.offer_id.map(pp.is_archived)
    df['name'] = df.offer_id.map(pp.name).fillna('')
    df['upd'] = pd.to_datetime(df.offer_id.map(pp.updated_at), utc=True).dt.tz_convert('Europe/Moscow').dt.strftime('%m-%d')
    df['len'] = df.offer_id.str.len()
    df['base4'] = df.offer_id.str[:4]
    df['five'] = df.offer_id.str[4].str.isdigit()
    df['set_n'] = df.name.str.extract(r'\((\d+)\s*шт', expand=False).fillna('1')
    df['kind'] = df.name.str.extract(r'^(\S+(?:\s\S+)?)', expand=False)
    df['brand'] = df.name.str.extract(r'(HP|Canon|Kyocera|Brother|Xerox|Samsung|Epson|Pantum|Ricoh|Sharp|Konica|OKI|Lexmark|Panasonic|Toshiba|Riso|Катюша)', flags=re.I, expand=False).str.upper().fillna('?')
for nm, df in (('НОВЫЕ', N), ('СТАРЫЕ', O)):
    p(f'--- {nm}: n={len(df)}  в ozon_product {df.in_prod.mean():.1%}  архив {df.archived.fillna(False).mean():.1%}  длина offer_id {df.len.value_counts().head(3).to_dict()}')
    p('  updated_at (MSK) топ:', df.upd.value_counts().head(6).to_dict())
    p('  набор (N шт):', df.set_n.value_counts().head(8).to_dict())
    p('  5-й символ цифра (ребёнок):', f'{df.five.mean():.1%}', ' тип:', df.kind.value_counts().head(5).to_dict())
    p('  бренд:', df.brand.value_counts(normalize=True).head(8).round(3).to_dict())
# пересечение базовых кодов
bo = set(O.base4); bn = set(N.base4)
p('новые: base4 уже есть среди старых acc2:', f'{N.base4.isin(bo).mean():.1%}', ' уникальных base4 новых', len(bn), 'из них новых кодов', len(bn - bo))
# дубли имени со старыми acc2
on = set(O.name)
p('новые: название совпадает со старой карточкой acc2:', f'{N.name.isin(on).mean():.1%}')
# дубли acc1 по имени / по коду
n1 = set(P1[~P1.is_archived].name); p('новые: название = активная карточка acc1:', f'{N.name.isin(n1).mean():.1%}', ' старые acc2 то же:', f'{O.name.isin(n1).mean():.1%}')
a1core = set(P1[~P1.is_archived].offer_id)
N['core'] = N.offer_id.map(core); O['core'] = O.offer_id.map(core)
# цены/индекс у новых на 14.09
t = pi[pi.d == '2026-09-14']; t = t.set_index('offer_id')
for nm, df in (('новые', N), ('старые', O)):
    x = t.reindex(df.offer_id)
    p(f'{nm} на 14.09: цена>0 {(x.price.astype(float) > 0).mean():.1%}  медиана цены {x.price.astype(float).median():.0f}  external_index есть {x.external_index.notna().mean():.1%}  color_index {x.color_index.value_counts(normalize=True).round(2).to_dict()}')
# продажи/остатки
PL = pd.read_pickle(DC + 'post_lines.pkl'); PL2 = PL[PL.account == 'oz_acc2']
p('новые с заказами в postings (когда-либо):', N.offer_id.isin(set(PL2.offer_id)).sum(), '; старые:', O.offer_id.isin(set(PL2.offer_id)).sum())
R = pd.read_pickle(C + 'real.pkl'); R2 = R[R.account == 'oz_acc2']
p('новые в реализации янв–авг:', N.offer_id.isin(set(R2.offer_id)).sum())
S = pd.read_pickle(DC + 'stock.pkl'); S['day'] = pd.to_datetime(S.day)
codes = set(S[S.day == S.day.max()].external_code)
p('остаток МС на', S.day.max().date(), 'по base4: новые', f'{N.base4.isin(codes).mean():.1%}', 'старые', f'{O.base4.isin(codes).mean():.1%}')
F = pd.read_pickle(DC + 'fbo.pkl');
SG = pd.read_pickle(C + 'signals.pkl'); p('новые в ozon_stock_signals:', N.offer_id.isin(set(SG.offer_id)).sum())
AT = pd.read_pickle(C + 'attrs_first.pkl'); A2 = AT[AT.account == 'oz_acc2']
af = pd.to_datetime(A2.groupby('offer_id').first_at.min(), utc=True).dt.tz_convert('Europe/Moscow').dt.strftime('%m-%d')
p('raw_ozon_attributes: новые найдены', N.offer_id.isin(af.index).sum(), 'first collected:', af.reindex(N.offer_id).value_counts().head(5).to_dict())
p('raw_ozon_attributes: старые first collected:', af.reindex(O.offer_id).value_counts().head(4).to_dict())
CS = pd.read_pickle(C + 'card_status.pkl'); CS2 = CS[CS.account == 'oz_acc2']
p('card_status acc2 новых:', N.offer_id.isin(set(CS2.offer_id)).sum())
# ozon_product acc2 offer_id вне индекса
notin = set(P2[~P2.is_archived].offer_id) - set(first.index)
p('активные ozon_product acc2 не в индексе вообще:', len(notin))
p('ozon_product acc2 активных', (~P2.is_archived).sum(), 'архив', P2.is_archived.sum())
# реклама
B = pd.read_pickle(C + 'bids_sku2.pkl')
p('новые в рекламе (ozon_bids, по sku продукта):', B[B.day.astype(str) == '2026-09-14'].sku.isin(set(P2[P2.offer_id.isin(new)].sku)).sum())
N.to_csv('newsku_acc2_2026-09-14.csv', index=False)
open('x2_newsku.txt', 'w').write('\n'.join(out)); print('\n'.join(out))

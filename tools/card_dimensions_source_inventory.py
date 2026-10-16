# поток: card
"""Inventory actual supplier fields directly from source files, without inferred sizes."""
import csv
import hashlib
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
import openpyxl
import xlrd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/card_all_supplier_dimensions_2026-10-15'
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
evidence = []
manifest = []

def number(v):
    try:
        n = float(str(v).strip().replace(' ', '').replace(',', '.'))
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, TypeError):
        return None

def triple(v):
    if isinstance(v, (list,tuple)):
        return list(v) if len(v)==3 and all(number(x) for x in v) else None
    parts = re.split(r'\s*[xх×*XХ]\s*', str(v).strip())
    return parts if len(parts)==3 and all(number(x) for x in parts) else None

def sparse_xlsx(path):
    # XML rows preserve original row numbers and avoid iterating Excel's styled million-row tail.
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            for event, el in ET.iterparse(z.open('xl/sharedStrings.xml'),events=('end',)):
                if el.tag == NS+'si':
                    strings.append(''.join(x.text or '' for x in el.iter(NS+'t')))
                    el.clear()
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        rels=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        targets={r.attrib['Id']:r.attrib['Target'] for r in rels}
        rid='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
        for sheet in wb.find(NS+'sheets'):
            target=targets[sheet.attrib[rid]]
            target=target.lstrip('/') if target.startswith('/') else 'xl/'+target
            for event, el in ET.iterparse(z.open(target),events=('end',)):
                if el.tag != NS+'row': continue
                values={}
                for cell in el.findall(NS+'c'):
                    col=re.sub(r'\d','',cell.attrib['r'])
                    v=cell.find(NS+'v')
                    value=v.text if v is not None else None
                    if cell.attrib.get('t')=='s' and value is not None: value=strings[int(value)]
                    if cell.attrib.get('t')=='inlineStr': value=''.join(t.text or '' for t in cell.iter(NS+'t'))
                    if value is not None: values[col]=value
                if values: yield sheet.attrib['name'],int(el.attrib['r']),values
                el.clear()

def source_info(path, supplier, kind, status):
    data={'supplier':supplier,'file':str(path.relative_to(ROOT)),'format':path.suffix,
          'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size,
          'file_mtime_utc':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
          'source_kind':kind,'status':status}
    manifest.append(data)
    return data

def add(info, sheet, row, article, values, cols, units, note='',brand=''):
    valid=bool(values and len(values)==3 and all(number(v) for v in values))
    evidence.append({'supplier':info['supplier'],'file':info['file'],'sheet':sheet,'row_or_item':row,
        'article':str(article).strip(),'brand':brand,'source_fields':cols,'units':units,
        'raw_dimensions':json.dumps(values,ensure_ascii=False),'three_positive_values':valid,'note':note})

def main():
    for filename,supplier in [('Изи.xlsx','Изипринт'),('Вся расходка Китай РФ Cactus GG PR.xlsx','Кактус'),('САКУРА АБДУЛ 05.06.xlsx','Сакура')]:
        path=ROOT/'incoming/gab'/filename
        info=source_info(path,supplier,'original_supplier_excel','explicit_dimensions')
        headers={}
        for sheet,r,v in sparse_xlsx(path):
            if r==1:
                headers[sheet]=v
                continue
            if supplier=='Кактус' and v.get('D'):
                add(info,sheet,r,v['D'],[v.get(c) for c in ['K','M','L']], 'K/M/L: Длина/Ширина/Высота (ИУ)','мм','Индивидуальная упаковка; МК исключён',v.get('B',''))
            elif supplier=='Изипринт':
                if sheet=='Данные по картриджам' and v.get('A'):
                    add(info,sheet,r,v['A'],triple(v.get('R')),'R: Размеры мм','мм', 'Исходная ячейка: '+str(v.get('R','')))
                elif sheet=='Чернила' and v.get('B'):
                    add(info,sheet,r,v['B'],triple(v.get('M')),'M: Размеры','не указаны','Единицы отдельно подтвердить; исходная ячейка: '+str(v.get('M','')))
            elif supplier=='Сакура':
                mapping={'Лазерная Sakura':('E',['Z','AA','AB'],'м'),
                         'Струйная Sakura':('A',['Z','AA','AB'],'мм'),
                         'Матричная Sakura':('A',['T','U','V'],'мм'),
                         'Наклейки SOK':('A',['AD','AE','AF'],'см')}
                if sheet in mapping:
                    ac,cols,units=mapping[sheet]
                    if v.get(ac): add(info,sheet,r,v[ac],[v.get(c) for c in cols],'/'.join(cols),units,'Явные поля коробки; не размеры ленты и не МК')
        info['headers']=headers
        print(supplier,'parsed',flush=True)

    for path in [ROOT/'incoming/gab/rapid_2026-07-18.json',ROOT/'docs/reports/card_other_supplier_dimensions_2026-10-14/rapid.json']:
        info=source_info(path,'Рапид','supplier_api_response','explicit_dimensions')
        d=json.loads(path.read_text());info['source_date']=d.get('date')
        for i,v in enumerate(d['price']):
            add(info,'price',i,v.get('CodeID',''),[v.get(k) for k in ['GabarityDlina','GabarityShirina','GabarityVisota']],
                'GabarityDlina/GabarityShirina/GabarityVisota','м','Индекс массива с нуля; ID='+str(v.get('ID')))

    path=ROOT/'incoming/gab/solutionsprint_dims.jsonl'
    info=source_info(path,'Спринт','extracted_website_fields','extracted_not_original_html')
    for i,line in enumerate(path.open(),1):
        v=json.loads(line)
        add(info,'jsonl',i,v.get('code',''),triple(v.get('dims_mm')),'dims_mm','мм',str(v.get('url','')))

    path=ROOT/'docs/reports/card_sprint_dimensions_prepare_2026-10-14/supplier_cards'
    start=len(evidence)
    for f in sorted(path.glob('*.json')):
        v=json.loads(f.read_text())
        info={'supplier':'Спринт (14.10)','file':str(f.relative_to(ROOT))}
        add(info,'properties',1,v.get('supplier_code',''),triple(v.get('properties',{}).get('Габариты, мм')),
            'Габариты, мм','мм',str(v.get('url','')))
    manifest.append({'supplier':'Спринт (14.10)','file':str(path.relative_to(ROOT)), 'source_kind':'extracted_website_fields',
        'status':'extracted_not_original_html','files':len(evidence)-start})

    path=ROOT/'incoming/gab/Профилайн.xls'
    info=source_info(path,'Профилайн','original_supplier_excel','no_dimensions_weight_volume_only')
    w=xlrd.open_workbook(path);info['sheets']=[{'name':s.name,'rows':s.nrows,'columns':s.ncols} for s in w.sheets()]
    for info in manifest:
        rows=[r for r in evidence if r['file']==info['file'] or (info.get('files') and r['file'].startswith(info['file']+'/'))]
        info['article_rows']=len(rows)
        info['three_positive_rows']=sum(r['three_positive_values'] for r in rows)
        info['unique_articles_with_three']=len({r['article'].upper() for r in rows if r['three_positive_values']})
        info['by_sheet']={s:{'article_rows':sum(r['sheet']==s for r in rows),'three_positive_rows':sum(r['sheet']==s and r['three_positive_values'] for r in rows)} for s in {r['sheet'] for r in rows}}
        if info['supplier']=='Кактус': info['brands_with_three']=dict(Counter(r['brand'] for r in rows if r['three_positive_values']))
    (OUT/'source_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    with (OUT/'source_rows.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(evidence[0]));w.writeheader();w.writerows(evidence)
    print('source_inventory complete',len(manifest),'sources',len(evidence),'article rows',flush=True)

if __name__=='__main__': main()

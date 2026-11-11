from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED
import re

ROOT = Path(__file__).resolve().parent
blocks = []
def title(s): blocks.append(('title', s))
def sub(s): blocks.append(('subtitle', s))
def h(s): blocks.append(('h1', s))
def h2(s): blocks.append(('h2', s))
def p(s): blocks.append(('p', s))
def item(s): blocks.append(('item', s))

exec((ROOT/'content.py').read_text())
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def run(t,bold=False):
 return '<w:r>'+('<w:rPr><w:b/></w:rPr>' if bold else '')+'<w:t xml:space="preserve">'+escape(t)+'</w:t></w:r>'
def para(text,style='Normal',bullet=False):
 return '<w:p><w:pPr><w:pStyle w:val="'+style+'"/>'+('<w:ind w:left="240" w:hanging="180"/>' if bullet else '')+'</w:pPr>'+run(('•  ' if bullet else '')+text)+'</w:p>'
def table(rows):
 out='<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>'+''.join(f'<w:{s} w:val="single" w:sz="4" w:color="CBD5E1"/>' for s in ['top','left','bottom','right','insideH','insideV'])+'</w:tblBorders><w:tblCellMar><w:top w:w="100" w:type="dxa"/><w:left w:w="120" w:type="dxa"/><w:bottom w:w="100" w:type="dxa"/><w:right w:w="120" w:type="dxa"/></w:tblCellMar></w:tblPr><w:tblGrid><w:gridCol w:w="7700"/><w:gridCol w:w="1900"/></w:tblGrid>'
 for i,row in enumerate(rows):
  out+='<w:tr><w:trPr><w:cantSplit/>'+('<w:tblHeader/>' if i==0 else '')+'</w:trPr>'
  for j,t in enumerate(row):
   strong=i in [0,len(rows)-1]
   out+='<w:tc><w:tcPr><w:tcW w:w="'+str(7700 if j==0 else 1900)+'" w:type="dxa"/>'+('<w:shd w:fill="EEF2F6"/>' if strong else '')+'</w:tcPr><w:p><w:pPr><w:spacing w:after="0"/>'+('<w:jc w:val="right"/>' if j else '')+'</w:pPr>'+run(t,strong)+'</w:p></w:tc>'
  out+='</w:tr>'
 return out+'</w:tbl>'
styles='''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="'''+W+'''">
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="260" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:pPr><w:widowControl/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="0" w:after="200"/></w:pPr><w:rPr><w:b/><w:color w:val="18354A"/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:after="100"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/><w:color w:val="334155"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="260" w:after="140"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/><w:color w:val="18354A"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:keepLines/><w:spacing w:before="180" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="23"/><w:color w:val="18354A"/></w:rPr></w:style>
</w:styles>'''
body=''
plain=[]
stylemap={'title':'Title','subtitle':'Subtitle','h1':'Heading1','h2':'Heading2','p':'Normal','item':'Normal'}
for typ,val in blocks:
 if typ=='table':
  body+=table(val); plain.extend(' | '.join(r) for r in val)
 else:
  body+=para(val,stylemap[typ],typ=='item'); plain.append(('• ' if typ=='item' else '')+val)
body+='<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1247" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>'
parts={
 '[Content_Types].xml':'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/></Types>''',
 '_rels/.rels':'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>''',
 'word/document.xml':'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="'+W+'" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'+body+'</w:body></w:document>',
 'word/styles.xml':styles,
 'word/_rels/document.xml.rels':'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>''',
 'word/footer1.xml':'<?xml version="1.0" encoding="UTF-8"?><w:ftr xmlns:w="'+W+'"><w:p><w:pPr><w:jc w:val="right"/></w:pPr><w:r><w:rPr><w:sz w:val="18"/><w:color w:val="64748B"/></w:rPr><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>'
}
txt='\n\n'.join(plain)+'\n'
assert sum(b for _,b in rows)==148
assert not re.search(r'мой\s*склад|moysklad|\bМС\b|цифров|второ[егй]|\b20\d{2}\b|\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b',txt,re.I)
for val in parts.values(): ET.fromstring(val)
docx=ROOT/'Отчёт_Печать_заказов_Дисквер.docx'
with ZipFile(docx,'w',ZIP_DEFLATED) as z:
 for name,content in parts.items(): z.writestr(name,content.encode('utf-8'))
(ROOT/'Отчёт_Печать_заказов_Дисквер.txt').write_text(txt)
with ZipFile(docx) as z:
 assert z.testzip() is None
 parsed=ET.fromstring(z.read('word/document.xml'))
 rendered='\n'.join(t.text or '' for t in parsed.iter('{'+W+'}t'))
 assert '148' in rendered
 assert not re.search(r'мой\s*склад|moysklad|\bМС\b|цифров|\b20\d{2}\b',rendered,re.I)
print('DOCX:',docx)
print('Size:',docx.stat().st_size,'bytes; words:',len(txt.split()))

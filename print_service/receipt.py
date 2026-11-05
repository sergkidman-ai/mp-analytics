# поток: print
"""Annotate the confirmed MS receipt template, preserving its existing content.

Only receipts pass through this module. Label PDFs must never be stamped.
Unknown layout raises an explicit error instead of placing text by guesswork.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

from fpdf import FPDF
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream

from print_service.domain import PrintBlocked
from print_service.pdf import pdf_reader

FONT = Path('/usr/share/fonts/truetype/dejavu')


def quantity_plan(word, words, left):
    """Fit a bold quantity inside its column; omit the frame when space is tight."""
    with TTFont(str(FONT / 'DejaVuSans-Bold.ttf')) as font:
        cmap, glyf, metrics = font.getBestCmap(), font['glyf'], font['hmtx'].metrics
        units, pen, bounds = font['head'].unitsPerEm, 0, []
        for char in word['text']:
            name = cmap[ord(char)];glyph = glyf[name]
            glyph.recalcBounds(glyf)
            bounds.append((pen + glyph.xMin, glyph.yMin, pen + glyph.xMax, glyph.yMax))
            pen += metrics[name][0]
    advance = pen / units
    gx0,gy0 = min(b[0] for b in bounds)/units,min(b[1] for b in bounds)/units
    gx1,gy1 = max(b[2] for b in bounds)/units,max(b[3] for b in bounds)/units
    nearby = [w for w in words if w is not word]
    right = min(((word['xMax']+w['xMin'])/2-.5 for w in nearby if w['xMin']>=word['xMax']
                 and abs(w['yMin']-word['yMin'])<4),default=left+42)
    center_x,center_y = (word['xMin']+word['xMax'])/2,(word['yMin']+word['yMax'])/2
    def collides(box):
        x0,y0,x1,y1=box
        return any(w['xMin']<x1 and w['xMax']>x0 and w['yMin']<y1 and w['yMax']>y0 for w in nearby)
    size=14;box_width=max(24,advance*size+8)
    if box_width<=right-left:
        x=max(left,min(center_x-box_width/2,right-box_width));y=center_y-10
        if not collides((x,y,x+box_width,y+20)):
            return dict(frame=True,x=x,y=y,width=box_width,height=20,size=size,
                        tx=x+(box_width-advance*size)/2,baseline=center_y+(gy0+gy1)*size/2)
    # A frame must never prevent printing an otherwise valid receipt. Keep
    # the text bold and fit its actual glyph bounds without covering a unit.
    fallback=min(8,(word['xMax']-word['xMin'])/advance,
                 (word['yMax']-word['yMin'])/(gy1-gy0))
    for size in (14,13,12,11,10,9,8,fallback):
        width=advance*size
        if width>right-left:continue
        x=max(left,min(center_x-width/2,right-width));baseline=center_y+(gy0+gy1)*size/2
        if not collides((x+gx0*size,baseline-gy1*size,x+gx1*size,baseline-gy0*size)):
            return dict(frame=False,size=size,tx=x,baseline=baseline)
    raise PrintBlocked('receipt_quantity_text_does_not_fit')


def word_pages(data):
    result = subprocess.run(['pdftotext', '-bbox-layout', '-', '-'], input=data,
                            capture_output=True, timeout=20)
    if result.returncode:
        raise PrintBlocked('receipt_text_layout_unavailable')
    doc = ET.fromstring(result.stdout)
    return [[dict(text=w.text or '', **{k: float(w.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax')})
             for w in page.iter() if w.tag.rsplit('}', 1)[-1] == 'word']
            for page in doc.iter() if page.tag.rsplit('}', 1)[-1] == 'page']


def compose(a, b):
    return (a[0]*b[0]+a[2]*b[1], a[1]*b[0]+a[3]*b[1],
            a[0]*b[2]+a[2]*b[3], a[1]*b[2]+a[3]*b[3],
            a[0]*b[4]+a[2]*b[5]+a[4], a[1]*b[4]+a[3]*b[5]+a[5])


def barcode_box(page, reader):
    matrix = (1, 0, 0, 1, 0, 0)
    stack, found = [], []
    for args, op in ContentStream(page.get_contents(), reader).operations:
        if op == b'q': stack.append(matrix)
        elif op == b'Q': matrix = stack.pop()
        elif op == b'cm': matrix = compose(matrix, tuple(float(x) for x in args))
        elif op == b'Do':
            obj = page['/Resources'].get('/XObject', {}).get(args[0])
            if obj is None: continue
            obj = obj.get_object()
            if obj.get('/Subtype') != '/Image': continue
            # MS varies barcode width with encoded value length. A short WB
            # value produces a much squarer image than an Ozon value. Identify
            # the actual repeated vertical bars, rather than the aspect ratio.
            pixels = page.images[str(args[0])].image.convert('L')
            if pixels.width < 40 or pixels.height < 10: continue
            rows = [[pixels.getpixel((col, int(pixels.height * fraction))) < 128
                     for col in range(pixels.width)] for fraction in (.25, .5, .75)]
            middle = rows[1]
            if sum(a != b for a, b in zip(middle, middle[1:])) < 20: continue
            if any(sum(a != b for a, b in zip(row, middle)) > pixels.width * .02 for row in rows): continue
            a, b, c, d, x, y = matrix
            if abs(b) > 0.01 or abs(c) > 0.01 or min(a, d) <= 0: continue
            found.append((x, float(page.mediabox.height) - y - d, a, d))
    if len(found) != 1:
        raise PrintBlocked('receipt_barcode_layout_ambiguous')
    return found[0]


def annotate_receipt(data, *, diskver=False, code=None, last_four=None, color=None, quantities=None):
    """code comes from Ozon scanit or WB partA, never from the order number.

    Ozon's full scanit is split into prefix and final four digits; WB provides
    partA and partB separately. Both print the tail below in large bold type.
    Diskver receipts omit the warehouse abbreviation. Other receipts print it
    black for the monochrome printer.

    quantities maps exact printed article -> units (>1) from shipment positions.
    White overlays replace the displayed signature/quantity; this is for paper
    output, not removal of the underlying PDF text for data redaction.
    """
    if diskver:
        color = None
    if code is not None and (not isinstance(code, str) or not code.strip()):
        raise PrintBlocked('marketplace_code_invalid')
    if code and last_four is None:
        code, last_four = code[:-4], code[-4:]
    if last_four is not None and (not code or not isinstance(last_four, str) or len(last_four) != 4 or not last_four.isdigit()):
        raise PrintBlocked('wb_sticker_parts_invalid')
    reader = pdf_reader(data)
    pages = word_pages(data)
    if len(pages) != len(reader.pages): raise PrintBlocked('receipt_layout_page_count_mismatch')
    marks = defaultdict(list)
    quantities = quantities or {}
    for article, quantity in quantities.items():
        if Decimal(str(quantity)) <= 1: continue
        matches = [(i, w) for i, words in enumerate(pages) for w in words if w['text'] == article]
        rows = {}
        for i, article_word in matches:
            headers = [w for w in pages[i] if w['text'] == 'Кол-во']
            if len(headers) != 1: raise PrintBlocked('receipt_quantity_header_ambiguous')
            left = headers[0]['xMin'] - 4
            article_headers = [w for w in pages[i] if w['text'] == 'Артикул']
            if len(article_headers) > 1: raise PrintBlocked('receipt_article_missing_or_ambiguous')
            if article_headers:
                header = article_headers[0]
                adjacent = [w for w in pages[i] if abs(w['yMin']-header['yMin'])<4]
                before = [w['xMax'] for w in adjacent if w['xMax']<header['xMin']]
                after = [w['xMin'] for w in adjacent if w['xMin']>header['xMax']]
                start = (max(before)+header['xMin'])/2 if before else 0
                end = (header['xMax']+min(after))/2 if after else left
                if article_word['yMin']<=header['yMax'] or not start<=article_word['xMin']<end:
                    continue  # A repeated code in a description/footer is not another item.
            candidates = {}
            for word in pages[i]:
                if not (left<=word['xMin']<=left+42 and abs(word['yMin']-article_word['yMin'])<4):continue
                try: value=Decimal(word['text'].replace(',','.'))
                except InvalidOperation:continue
                if value.is_finite() and value>0:
                    key=(i,word['xMin'],word['yMin'],word['xMax'],word['yMax'])
                    candidates[key]=(word,value,left)
            if len(candidates)!=1:raise PrintBlocked('receipt_quantity_does_not_match_shipment')
            rows.update(candidates)
        if not rows:raise PrintBlocked('receipt_article_missing_or_ambiguous')
        if sum(value for word,value,left in rows.values())!=Decimal(str(quantity)):
            raise PrintBlocked('receipt_quantity_does_not_match_shipment')
        for key,(word,value,left) in rows.items():
            if value>1:marks[key[0]].append((word,quantity_plan(word,pages[key[0]],left)))
    signatures = [(i, w) for i, words in enumerate(pages) for w in words if w['text'] == 'Простакова']
    if diskver and len(signatures) != 1: raise PrintBlocked('diskver_signature_layout_ambiguous')
    writer = PdfWriter()
    for i, source in enumerate(reader.pages):
        if source.rotation or float(source.mediabox.left) or float(source.mediabox.bottom):
            raise PrintBlocked('receipt_page_coordinates_unsupported')
        width, height = float(source.mediabox.width), float(source.mediabox.height)
        stamp = FPDF(unit='pt', format=(width, height))
        stamp.set_auto_page_break(False)
        stamp.set_margins(0, 0, 0)
        stamp.add_font('Print', fname=str(FONT / 'DejaVuSans.ttf'))
        stamp.add_font('Print', style='B', fname=str(FONT / 'DejaVuSans-Bold.ttf'))
        stamp.add_page()
        stamp.set_font('Print', size=9)
        if diskver and signatures[0][0] == i:
            word = signatures[0][1]
            tail = [w for w in pages[i] if w['xMin'] >= word['xMin'] and abs(w['yMin'] - word['yMin']) < 2]
            right = max(w['xMax'] for w in tail)
            stamp.set_fill_color(255)
            stamp.rect(word['xMin'] - 1, word['yMin'] - 2, right - word['xMin'] + 4, word['yMax'] - word['yMin'] + 5, style='F')
            stamp.text(word['xMin'], word['yMax'] - 1, 'Терещенко С.С.')
        for word, plan in marks[i]:
            stamp.set_fill_color(255)
            stamp.rect(word['xMin'],word['yMin'],word['xMax']-word['xMin'],word['yMax']-word['yMin'],style='F')
            stamp.set_font('Print', style='B', size=plan['size'])
            if plan['frame']:
                stamp.rect(plan['x'],plan['y'],plan['width'],plan['height'],style='DF')
            stamp.text(plan['tx'],plan['baseline'],word['text'])
        if i == 0 and (code or color):
            x, top, bw, bh = barcode_box(source, reader)
            if code:
                tx = x + bw + 6
                stamp.set_font('Print', size=10)
                if stamp.get_string_width(code) > width - tx - 8:
                    stamp.set_font('Print', size=8)
                if stamp.get_string_width(code) > width - tx - 8:
                    raise PrintBlocked('receipt_code_does_not_fit_next_to_barcode')
                stamp.text(tx, top + 10, code)
                if last_four:
                    stamp.set_font('Print', style='B', size=22)
                    if stamp.get_string_width(last_four) > width - tx - 8:
                        raise PrintBlocked('receipt_code_tail_does_not_fit_next_to_barcode')
                    stamp.text(tx, top + 34, last_four)
            if color:
                stamp.set_font('Print', style='B', size=14)
                stamp.set_text_color(0)
                stamp.set_draw_color(0)
                stamp.rect(x - 65, top, 50, 24)
                stamp.text(x - 59, top + 17, color['short'])
        try:
            overlay = PdfReader(BytesIO(bytes(stamp.output()))).pages[0]
        finally:
            for font in stamp.fonts.values():
                if hasattr(font, 'ttfont'):
                    font.ttfont.close()
        target = writer.add_page(source)
        target.merge_page(overlay)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()

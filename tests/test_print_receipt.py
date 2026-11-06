# поток: print
from io import BytesIO
import unittest

from fpdf import FPDF
from PIL import Image, ImageDraw
from pypdf import PdfReader
from pypdf.generic import ContentStream

from print_service.domain import PrintBlocked
from print_service.receipt import annotate_receipt, quantity_plan, word_pages


def receipt(*, signature=True, barcode=True, narrow_barcode=False, compact_quantity=False, row_quantities=None, duplicate_description=False):
    pdf = FPDF(unit='pt', format=(612, 792))
    pdf.add_font('Print', fname='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font('Print', size=9)
    pdf.text(60, 140, 'Товарный чек № TEST-123')
    pdf.text(80, 170, 'Артикул')
    if duplicate_description:pdf.text(180,170,'Наименование')
    pdf.text(370, 170, 'Кол-во')
    for i,quantity in enumerate(row_quantities or [10]):
        y=205+i*35
        pdf.text(80,y,'TEST-SKU');pdf.text(374,y,str(quantity))
        if compact_quantity:pdf.text(388,y,'шт')
        if duplicate_description:pdf.text(180,y,'TEST-SKU')
    if signature:
        pdf.text(410, 400, 'Простакова Н.Ю.')
    if barcode:
        # Layout fixture only; not a barcode used for a real shipment.
        img = Image.new('1', (800, 320 if narrow_barcode else 100), 1)
        draw = ImageDraw.Draw(img)
        for x in range(0, 800, 8): draw.rectangle((x, 0, x+3, img.height-1), fill=0)
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        pdf.image(BytesIO(buffer.getvalue()), x=340, y=80, w=160, h=30)
    data = bytes(pdf.output())
    for font in pdf.fonts.values():
        if hasattr(font, 'ttfont'):
            font.ttfont.close()
    return data


class PrintReceiptTests(unittest.TestCase):
    def test_native_express_identity_highlight_and_partner_without_scanit(self):
        from print_service.receipt import express_receipt_code
        number='0210482468-0008-1'
        def label(tails):
            pdf=FPDF(unit='pt',format=(164,114));pdf.set_auto_page_break(False);pdf.add_page()
            pdf.set_font('Helvetica',size=8);pdf.text(8,20,number)
            pdf.set_font('Helvetica',style='B',size=24)
            for i,tail in enumerate(tails):pdf.text(8,50+i*25,tail)
            return bytes(pdf.output())
        self.assertEqual(express_receipt_code(label(['2468']),number),(number,'2468'))
        self.assertEqual(express_receipt_code(label([]),number),(number,None))
        with self.assertRaises(PrintBlocked):express_receipt_code(label(['2468']),'123-0001-1')
        with self.assertRaises(PrintBlocked):express_receipt_code(label(['2468','9999']),number)
        raw=receipt()
        result=annotate_receipt(raw,code=number,split_code_tail=False)
        self.assertIn(number,PdfReader(BytesIO(result)).pages[0].extract_text())
        result=annotate_receipt(raw,code=number,last_four='2468',split_code_tail=False)
        self.assertIn('2468',PdfReader(BytesIO(result)).pages[0].extract_text())

    def test_repeated_article_rows_keep_their_actual_quantities(self):
        source=receipt(row_quantities=[1,1],signature=False,barcode=False)
        result=annotate_receipt(source,quantities={'TEST-SKU':2})
        words=word_pages(result)[0]
        self.assertEqual(sum(w['text']=='1' for w in words),2)
        self.assertFalse(any(w['text']=='2' for w in words))
        self.assertEqual(sum(w['text']=='TEST-SKU' for w in words),2)

    def test_repeated_article_highlights_each_row_and_checks_the_total(self):
        source=receipt(row_quantities=[2,3],signature=False,barcode=False)
        result=annotate_receipt(source,quantities={'TEST-SKU':5});spans=[]
        def visit(t,cm,tm,font,size):
            if t.strip():spans.append((t.strip(),str((font or {}).get('/BaseFont',''))))
        PdfReader(BytesIO(result)).pages[0].extract_text(visitor_text=visit)
        self.assertEqual({t for t,f in spans if 'Bold' in f},{'2','3'})
        with self.assertRaisesRegex(PrintBlocked,'receipt_quantity_does_not_match_shipment'):
            annotate_receipt(source,quantities={'TEST-SKU':6})

    def test_article_in_description_is_not_counted_as_an_extra_item(self):
        source=receipt(duplicate_description=True,signature=False,barcode=False)
        annotate_receipt(source,quantities={'TEST-SKU':10})

    def test_tight_quantity_column_uses_bold_without_frame_and_keeps_unit(self):
        source=receipt(signature=False,barcode=False,compact_quantity=True)
        words=word_pages(source)[0];word=next(w for w in words if w['text']=='10')
        header=next(w for w in words if w['text']=='Кол-во')
        self.assertFalse(quantity_plan(word,words,header['xMin']-4)['frame'])
        result=annotate_receipt(source,quantities={'TEST-SKU':10});reader=PdfReader(BytesIO(result))
        spans=[]
        def visit(text,cm,tm,font,size):
            if text.strip():spans.append((text.strip(),str((font or {}).get('/BaseFont','')),size))
        text=reader.pages[0].extract_text(visitor_text=visit)
        self.assertIn('шт',text);self.assertTrue(any(t=='10' and 'Bold' in f for t,f,s in spans))
        self.assertFalse(any(op in (b'B',b'B*',b'S') for args,op in ContentStream(reader.pages[0].get_contents(),reader).operations))

    def test_quantity_frame_is_kept_when_it_fits(self):
        source=receipt(signature=False,barcode=False)
        result=annotate_receipt(source,quantities={'TEST-SKU':10});reader=PdfReader(BytesIO(result))
        self.assertTrue(any(op==b'B' for args,op in ContentStream(reader.pages[0].get_contents(),reader).operations))

    def test_short_wb_barcode_is_identified_by_bars_not_image_ratio(self):
        data = annotate_receipt(receipt(narrow_barcode=True), code='7654321', last_four='0789')
        text = PdfReader(BytesIO(data)).pages[0].extract_text()
        self.assertIn('7654321', text)
        self.assertIn('0789', text)

    def test_ozon_tail_on_separate_line_large_bold_and_black_warehouse(self):
        data = annotate_receipt(receipt(), code='ii50116951429',
                                color={'short': 'Син', 'css': '#0000ff'})
        reader = PdfReader(BytesIO(data))
        spans = []
        def visit(text, cm, tm, font, size):
            if text.strip(): spans.append((text.strip(), str((font or {}).get('/BaseFont', '')), size, tm[5]))
        text = reader.pages[0].extract_text(visitor_text=visit)
        self.assertIn('ii5011695', text)
        self.assertIn('1429', text)
        self.assertIn('Син', text)
        self.assertNotIn('ii50116951429', text)
        prefix = next(s for s in spans if s[0] == 'ii5011695')
        tail = next(s for s in spans if s[0] == '1429')
        self.assertIn('Bold', tail[1])
        self.assertEqual(tail[2], 22)
        self.assertLess(tail[3], prefix[3])
        for args, op in ContentStream(reader.pages[0].get_contents(), reader).operations:
            if op in (b'rg', b'RG'):
                self.assertEqual(tuple(float(x) for x in args), (0, 0, 0))

    def test_numeric_ozon_qr_payload_keeps_exact_prefix_and_bold_tail(self):
        data=annotate_receipt(receipt(),code='855800012341000')
        spans=[]
        def visit(text,cm,tm,font,size):
            if text.strip():spans.append((text.strip(),str((font or {}).get('/BaseFont','')),size))
        text=PdfReader(BytesIO(data)).pages[0].extract_text(visitor_text=visit)
        self.assertIn('85580001234',text);self.assertIn('1000',text)
        tail=next(s for s in spans if s[0]=='1000')
        self.assertIn('Bold',tail[1]);self.assertEqual(tail[2],22)

    def test_diskver_signature_code_quantity_without_warehouse_color(self):
        data = annotate_receipt(receipt(), diskver=True, code='7654321', last_four='0789',
                                color={'short': 'Син', 'css': '#0000ff'}, quantities={'TEST-SKU': 10})
        result = PdfReader(BytesIO(data))
        text = result.pages[0].extract_text()
        self.assertIn('Терещенко С.С.', text)
        self.assertIn('7654321', text)
        self.assertIn('0789', text)
        self.assertNotIn('Син', text)
        self.assertIn('TEST-123', text)
        self.assertEqual(len(result.pages[0].images), 1)

    def test_mismatched_quantity_and_unrecognized_layout_block_printing(self):
        with self.assertRaises(PrintBlocked):
            annotate_receipt(receipt(), quantities={'TEST-SKU': 11})
        with self.assertRaises(PrintBlocked):
            annotate_receipt(receipt(signature=False), diskver=True)
        with self.assertRaises(PrintBlocked):
            annotate_receipt(receipt(barcode=False), code='7654321')

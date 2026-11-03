# поток: print
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch

from fpdf import FPDF
from PIL import Image, ImageDraw
from pypdf import PdfReader

from print_service.diskver_design import (PHONE, STYLES, SUPPORT_QR_SCALE,
    apply_diskver_design, original_qr, qr_asset)
from print_service.domain import PrintBlocked


def fixture(*, organization='ДИСКВЭР', overlapping_text=False, late_text=False):
    banner = Image.new('RGB', (793, 226), 'white')
    ImageDraw.Draw(banner).rectangle((5, 5, 780, 220), outline='black', width=3)
    stream = BytesIO()
    banner.save(stream, format='PNG')
    asset, metadata = qr_asset()
    metadata = dict(metadata, source_image_pixels_sha256=sha256(banner.tobytes()).hexdigest())
    pdf = FPDF(unit='pt', format=(612, 792))
    pdf.add_font('Print', fname='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font('Print', size=9)
    pdf.text(300, 50, 'ООО "' + organization + '"')
    pdf.text(170, 147, 'Товарный чек № TEST-42')
    pdf.text(60, 220, 'SKU-42   2 шт   100,00')
    pdf.text(400, 400, 'Терещенко С.С.')
    pdf.text(520, 105, 'ii5011695')
    pdf.text(520, 128, '1429')
    if overlapping_text:
        pdf.text(60, 560, 'Обязательный текст документа')
    if late_text:
        pdf.text(60, 740, 'Нет места для блока поддержки')
    pdf.image(BytesIO(stream.getvalue()), x=50.4, y=562.4, w=465.3, h=125.4)
    data = bytes(pdf.output())
    for font in pdf.fonts.values():
        if hasattr(font, 'ttfont'): font.ttfont.close()
    return data, asset, metadata


class DiskverDesignTests(unittest.TestCase):
    def test_selected_style_has_telegram_only_and_enlarges_original_qr(self):
        source, asset, metadata = fixture()
        with patch('print_service.diskver_design.qr_asset', return_value=(asset, metadata)):
            with patch('print_service.diskver_design.original_qr', wraps=original_qr) as placed:
                output = apply_diskver_design(source)
        text = PdfReader(BytesIO(output)).pages[0].extract_text()
        self.assertNotIn(PHONE, text)
        self.assertIn('Telegram', text)
        self.assertEqual(placed.call_count, 1)
        sizes = placed.call_args.args[2]['physical_size_pt']
        for actual, native in zip(sizes, metadata['physical_size_pt']):
            self.assertAlmostEqual(actual / native, SUPPORT_QR_SCALE)

    def test_all_styles_preserve_original_qr_pixels_and_receipt_fields(self):
        source, asset, metadata = fixture()
        with patch('print_service.diskver_design.qr_asset', return_value=(asset, metadata)):
            for style in STYLES:
                with self.subTest(style=style):
                    output = apply_diskver_design(source, style=style)
                    page = PdfReader(BytesIO(output)).pages[0]
                    text = page.extract_text()
                    for expected in ('TEST-42', 'SKU-42', '100,00', 'Терещенко С.С.', 'ii5011695', '1429'):
                        self.assertIn(expected, text)
                    self.assertTrue(any(
                        list(image.image.size) == metadata['pixel_size']
                        and sha256(image.image.convert('RGBA').tobytes()).hexdigest() == metadata['pixels_sha256']
                        for image in page.images))
                    self.assertEqual(list(page.mediabox), [0, 0, 612, 792])
                    self.assertEqual(output, apply_diskver_design(source, style=style))

    def test_foreign_organization_and_unknown_style_are_rejected(self):
        source, _, _ = fixture(organization='ЦИФРОВОЙ КВАДРАТ')
        with self.assertRaises(PrintBlocked):
            apply_diskver_design(source, style='brackets')
        with self.assertRaises(PrintBlocked):
            apply_diskver_design(source, style='unknown')

    def test_footer_moves_below_live_document_text(self):
        source, asset, metadata = fixture(overlapping_text=True)
        with patch('print_service.diskver_design.qr_asset', return_value=(asset, metadata)):
            output = apply_diskver_design(source, style='service')
        text = PdfReader(BytesIO(output)).pages[0].extract_text()
        self.assertIn('Обязательный текст документа', text)
        self.assertIn('Поможем с вашим картриджем', text)

    def test_no_room_for_footer_blocks_generation(self):
        source, asset, metadata = fixture(late_text=True)
        with patch('print_service.diskver_design.qr_asset', return_value=(asset, metadata)):
            with self.assertRaises(PrintBlocked):
                apply_diskver_design(source, style='service')

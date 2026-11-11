# поток: print
"""Repeatable Diskver receipt proposals: vector decorations + original QR pixels.

This is an optional design step after receipt annotation and before A4 output.
The user selected the brackets style. Shipment labels never enter this module.
"""
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from fpdf import FPDF
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream

from print_service.domain import PrintBlocked
from print_service.pdf import pdf_reader
from print_service.receipt import FONT, compose, word_pages

ASSETS = Path(__file__).with_name('assets')
STYLES = {
    'brackets': '1. Уголки DSquare',
    'cards': '2. Контактные карточки',
    'service': '3. Сервисная полоса',
}
SELECTED_STYLE = 'brackets'
SUPPORT_QR_SCALE = 1.55
PHONE = '8 800 333 43 87'


def qr_asset():
    metadata = json.loads((ASSETS / 'diskver_support_qr.json').read_text())
    path = ASSETS / 'diskver_support_qr.png'
    with Image.open(path) as image:
        if (list(image.size) != metadata['pixel_size']
                or sha256(image.tobytes()).hexdigest() != metadata['pixels_sha256']
                or sha256(image.crop(metadata['core_box_in_asset']).tobytes()).hexdigest()
                != metadata['source_core_pixels_sha256']):
            raise PrintBlocked('support_qr_asset_changed')
    return path, metadata


def support_box(page, reader, metadata):
    names = set()
    for image in page.images:
        if (list(image.image.size) == metadata['source_image_size']
                and sha256(image.image.tobytes()).hexdigest() == metadata['source_image_pixels_sha256']):
            names.add(Path(image.name).stem)
    matrix, stack, boxes = (1, 0, 0, 1, 0, 0), [], []
    for args, op in ContentStream(page.get_contents(), reader).operations:
        if op == b'q': stack.append(matrix)
        elif op == b'Q': matrix = stack.pop()
        elif op == b'cm': matrix = compose(matrix, tuple(float(x) for x in args))
        elif op == b'Do' and str(args[0]).lstrip('/') in names:
            a, b, c, d, x, y = matrix
            if abs(b) > 0.01 or abs(c) > 0.01 or min(a, d) <= 0:
                raise PrintBlocked('support_banner_transform_unsupported')
            if not 400 <= a <= 540 or not 100 <= d <= 200:
                raise PrintBlocked('support_banner_geometry_unsupported')
            boxes.append((x, float(page.mediabox.height) - y - d, a, d))
    if len(boxes) > 1:
        raise PrintBlocked('support_banner_ambiguous')
    return boxes[0] if boxes else None


def remove_support_image(page, writer, metadata):
    names = {Path(image.name).stem for image in page.images
             if list(image.image.size) == metadata['source_image_size']
             and sha256(image.image.tobytes()).hexdigest() == metadata['source_image_pixels_sha256']}
    stream = ContentStream(page.get_contents(), writer)
    original = len(stream.operations)
    stream.operations = [(args, op) for args, op in stream.operations
                         if not (op == b'Do' and str(args[0]).lstrip('/') in names)]
    if original - len(stream.operations) != 1:
        raise PrintBlocked('support_banner_removal_ambiguous')
    page.replace_contents(stream)


def brackets(pdf, x, y, width, height, *, cap=7, line=1.5):
    pdf.set_line_width(line)
    for xx, direction in ((x, 1), (x + width, -1)):
        pdf.line(xx, y, xx, y + height)
        pdf.line(xx, y, xx + direction * cap, y)
        pdf.line(xx, y + height, xx + direction * cap, y + height)


def corner_brackets(pdf, x, y, width, height, *, arm=14, line=1.8):
    """Four equal L-shaped corners, with open gaps between the arms."""
    if arm <= 0 or arm * 2 >= min(width, height):
        raise PrintBlocked('corner_bracket_geometry_invalid')
    pdf.set_line_width(line)
    for xx, dx in ((x, 1), (x + width, -1)):
        for yy, dy in ((y, 1), (y + height, -1)):
            pdf.line(xx, yy, xx + dx * arm, yy)
            pdf.line(xx, yy, xx, yy + dy * arm)


def text(pdf, x, y, value, size=10, bold=False):
    pdf.set_font('Print', style='B' if bold else '', size=size)
    pdf.text(x, y, value)


def original_qr(pdf, asset, metadata, x, y):
    width, height = metadata['physical_size_pt']
    pdf.image(str(asset), x=x, y=y, w=width, h=height)
    # Use the original decoded destination for the PDF hyperlink too.
    destination = metadata['payload']
    if destination.startswith(('https://', 'http://')):
        pdf.link(x, y, width, height, destination)


def draw_footer(pdf, box, style, asset, metadata):
    x, y, width, height = box
    qr_w, qr_h = metadata['physical_size_pt']
    pdf.set_fill_color(255)
    pdf.rect(x - 1, y - 1, width + 2, height + 2, style='F')
    pdf.set_text_color(0)
    pdf.set_draw_color(0)
    if style == 'brackets':
        corner_brackets(pdf, x, y + 3, width, height - 6)
        text(pdf, x + 20, y + 48, 'ПОДДЕРЖКА DSQUARE', 14, True)
        text(pdf, x + 20, y + 71, 'Вопрос по картриджу?', 11)
        text(pdf, x + 20, y + 89, 'Напишите нам в Telegram', 11)
        qx = x + width - qr_w - 20
        original_qr(pdf, asset, metadata, qx, y + 12)
        pdf.set_font('Print', size=9)
        caption_width = pdf.get_string_width('Telegram')
        text(pdf, qx + (qr_w - caption_width) / 2, y + qr_h + 26, 'Telegram', 9)
    elif style == 'cards':
        gap, right = 12, 130
        left = width - gap - right
        pdf.set_line_width(0.65)
        pdf.rect(x, y + 3, left, height - 6)
        pdf.rect(x + left + gap, y + 3, right, height - 6)
        brackets(pdf, x, y + 3, left, height - 6, cap=10, line=1.8)
        text(pdf, x + 17, y + 25, '[ DSQUARE ]', 10, True)
        text(pdf, x + 17, y + 51, 'Нужна помощь?', 17, True)
        text(pdf, x + 17, y + 81, PHONE, 17, True)
        text(pdf, x + 17, y + 101, 'Телефон поддержки', 9)
        qx = x + left + gap + (right - qr_w) / 2
        original_qr(pdf, asset, metadata, qx, y + 17)
        text(pdf, x + left + gap + 38, y + max(101, 17 + qr_h + 12), 'Telegram', 9)
    else:
        pdf.set_line_width(1.3)
        pdf.line(x, y + 2, x + width, y + 2)
        text(pdf, x, y + 26, 'DSQUARE', 18, True)
        text(pdf, x + width - 101, y + 24, 'ПОДДЕРЖКА', 10, True)
        original_qr(pdf, asset, metadata, x + 5, y + 46)
        brackets(pdf, x, y + 40, qr_w + 10, qr_h + 12, cap=6, line=1)
        tx = x + qr_w + 27
        text(pdf, tx, y + 65, 'Поможем с вашим картриджем', 12, True)
        text(pdf, tx, y + 88, PHONE, 17, True)
        text(pdf, tx, y + 109, 'Сканируйте QR — откроется Telegram', 9)


def apply_diskver_design(data, *, style=SELECTED_STYLE):
    if style not in STYLES:
        raise PrintBlocked('unknown_diskver_design')
    reader = pdf_reader(data)
    if 'ДИСКВЭР' not in (reader.pages[0].extract_text() or '').upper():
        raise PrintBlocked('diskver_design_organization_mismatch')
    asset, metadata = qr_asset()
    layouts = word_pages(data)
    writer, replaced = PdfWriter(), 0
    for index, source in enumerate(reader.pages):
        if source.rotation or float(source.mediabox.left) or float(source.mediabox.bottom):
            raise PrintBlocked('diskver_design_page_coordinates_unsupported')
        box = support_box(source, reader, metadata)
        if not box:
            writer.add_page(source)
            continue
        x, y, width, height = box
        document_metadata = dict(metadata, physical_size_pt=[
            metadata['pixel_size'][0] * width / metadata['source_image_size'][0],
            metadata['pixel_size'][1] * height / metadata['source_image_size'][1]])
        if style == 'brackets':
            document_metadata['physical_size_pt'] = [
                size * SUPPORT_QR_SCALE for size in document_metadata['physical_size_pt']]
        # MS may stretch the same banner differently as item rows change.
        # Use the source QR footprint as a base (the selected style enlarges
        # it by the user-approved factor), remove only the image draw, and
        # place the new footer below all live text rather than painting over it.
        y = max(y, max(w['yMax'] for w in layouts[index]) + 18)
        qr_space = 38 if style == 'brackets' else 52
        height = max(125.4, document_metadata['physical_size_pt'][1] + qr_space)
        if y + height > float(source.mediabox.height) - 18:
            raise PrintBlocked('support_design_has_no_clear_print_area')
        box = (x, y, width, height)
        stamp = FPDF(unit='pt', format=(float(source.mediabox.width), float(source.mediabox.height)))
        stamp.set_margins(0, 0, 0)
        stamp.set_auto_page_break(False)
        stamp.set_creation_date(datetime(2000, 1, 1, tzinfo=timezone.utc))
        stamp.add_font('Print', fname=str(FONT / 'DejaVuSans.ttf'))
        stamp.add_font('Print', style='B', fname=str(FONT / 'DejaVuSans-Bold.ttf'))
        stamp.add_page()
        try:
            draw_footer(stamp, box, style, asset, document_metadata)
            title = next((w for w in layouts[index] if w['text'] == 'Товарный'), None)
            if title:
                line = [w for w in layouts[index] if abs(w['yMin'] - title['yMin']) < 2]
                left, right = min(w['xMin'] for w in line), max(w['xMax'] for w in line)
                if style == 'brackets':
                    corner_brackets(stamp, left - 12, title['yMin'] - 4,
                                    right - left + 24, 18, arm=4, line=1)
                elif style == 'cards':
                    brackets(stamp, x - 7, 60, 157, 53, cap=6, line=1)
                    stamp.set_line_width(0.7)
                    stamp.line(x, title['yMax'] + 6, x + width, title['yMax'] + 6)
                else:
                    brackets(stamp, left - 12, title['yMin'] - 4, right - left + 24, 18, cap=4, line=0.7)
                    stamp.set_line_width(1)
                    stamp.line(x - 15, 65, x - 15, 113)
                    stamp.line(x - 15, 65, x - 9, 65)
                    stamp.line(x - 15, 113, x - 9, 113)
            overlay = PdfReader(BytesIO(bytes(stamp.output()))).pages[0]
            target = writer.add_page(source)
            remove_support_image(target, writer, metadata)
            target.merge_page(overlay)
            replaced += 1
        finally:
            for font in stamp.fonts.values():
                if hasattr(font, 'ttfont'):
                    font.ttfont.close()
    if not replaced:
        raise PrintBlocked('confirmed_support_banner_not_found')
    writer.add_metadata({'/Title': 'Дисквер — ' + STYLES[style]})
    output = BytesIO()
    writer.write(output)
    return output.getvalue()

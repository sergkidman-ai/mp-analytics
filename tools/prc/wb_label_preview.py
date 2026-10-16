# поток: prc
"""Build a labelled PDF geometry preview, not a production label or MS template."""
import argparse
import csv
import gzip
import json
from pathlib import Path
import sys
import zlib

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fontTools.ttLib import TTFont
from prices.wb_label import text_width, wrap_lines, LINE_WIDTH_PT, FONT_SIZE_PT

PT_MM = 72 / 25.4


class PDF:
    def __init__(self, font_path):
        self.objects = [b'', b'']  # root, pages
        font = TTFont(font_path)
        cmap = font.getBestCmap()
        glyph_map = bytearray(65536 * 2)
        widths = []
        for cp, glyph in cmap.items():
            if cp > 65535:
                continue
            glyph_map[cp * 2:cp * 2 + 2] = font.getGlyphID(glyph).to_bytes(2, 'big')
            widths.append(f"{cp} [{font['hmtx'].metrics[glyph][0]}]")
        file_id = self.stream(Path(font_path).read_bytes(), '/Length1 ' + str(Path(font_path).stat().st_size))
        head = font['head']
        desc = self.add(f'<< /Type /FontDescriptor /FontName /NotoSans /Flags 32 /ItalicAngle 0 '
                        f'/Ascent 1069 /Descent -293 /CapHeight 714 /StemV 80 '
                        f'/FontBBox [{head.xMin} {head.yMin} {head.xMax} {head.yMax}] '
                        f'/FontFile2 {file_id} 0 R >>')
        mapping = self.stream(bytes(glyph_map))
        cid = self.add('<< /Type /Font /Subtype /CIDFontType2 /BaseFont /NotoSans '
                       '/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> '
                       f'/FontDescriptor {desc} 0 R /DW 1000 /W [{" ".join(widths)}] '
                       f'/CIDToGIDMap {mapping} 0 R >>')
        unicode = self.stream(b'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                              b'/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                              b'/CMapName /Identity-UCS def /CMapType 2 def\n'
                              b'1 begincodespacerange <0000> <FFFF> endcodespacerange\n'
                              b'1 beginbfrange <0000> <FFFF> <0000> endbfrange\n'
                              b'endcmap CMapName currentdict /CMap defineresource pop end end')
        self.font_id = self.add('<< /Type /Font /Subtype /Type0 /BaseFont /NotoSans '
                                f'/Encoding /Identity-H /DescendantFonts [{cid} 0 R] /ToUnicode {unicode} 0 R >>')
        self.pages = []

    def add(self, data):
        self.objects.append(data.encode() if isinstance(data, str) else data)
        return len(self.objects)

    def stream(self, data, extra=''):
        data = zlib.compress(data)
        return self.add(f'<< /Length {len(data)} /Filter /FlateDecode {extra} >>\nstream\n'.encode()
                        + data + b'\nendstream')

    def page(self, commands):
        content = self.stream('\n'.join(commands).encode())
        self.pages.append(self.add(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.276 841.89] '
                                   f'/Resources << /Font << /F {self.font_id} 0 R >> >> '
                                   f'/Contents {content} 0 R >>'))

    def write(self, path):
        self.objects[0] = b'<< /Type /Catalog /Pages 2 0 R >>'
        self.objects[1] = f'<< /Type /Pages /Count {len(self.pages)} /Kids [{" ".join(f"{n} 0 R" for n in self.pages)}] >>'.encode()
        data = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n');offsets = [0]
        for i, obj in enumerate(self.objects, 1):
            offsets.append(len(data));data += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
        xref = len(data);data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
        for offset in offsets[1:]:
            data += f'{offset:010d} 00000 n \n'.encode()
        data += f'trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
        path.write_bytes(data)


def text(x, y, value, size=9):
    encoded = value.encode('utf-16-be').hex().upper()
    return f'BT /F {size} Tf 1 0 0 1 {x:.3f} {y:.3f} Tm <{encoded}> Tj ET'


def label(x, y, name, code, warning=False):
    w, h = 58 * PT_MM, 40 * PT_MM
    commands = [f'q {x} {y} {w} {h} re W n', f'0.5 G {x} {y} {w} {h} re S', '0 g',
                text(x + 7, y + h - 11, 'РАСЧЁТНЫЙ МАКЕТ / НЕ ДЛЯ ПЕЧАТИ', 6),
                text(x + 7, y + h - 29, code, 9),
                f'0.8 G {x + 12} {y + 39} {w - 24} 35 re S', '0.5 g',
                text(x + 28, y + 54, 'Зона штрихкода', 8), '0 g']
    rows = wrap_lines(name)
    if rows is None:
        commands.extend([text(x + 6, y + 22, 'НЕ ПОМЕЩАЕТСЯ БЕЗ ОБРЕЗКИ', 7),
                         text(x + 6, y + 11, 'модели или составного цвета', 7)])
    else:
        for i, row in enumerate(rows):
            width = text_width(row)
            commands.append(text(x + (w - width) / 2, y + 16.4 - i * 11, row, FONT_SIZE_PT))
    commands.append('Q')
    if warning:
        commands.extend(['0.7 0 0 rg', text(x, y - 10, 'Третья строка выходит за край / HOLD', 7), '0 g'])
    return commands


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    with gzip.open(args.out / 'dry_run/review.csv.gz', 'rt', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    by_id = {r['ms_id']: r for r in rows}
    samples = []
    originals = json.loads((ROOT / 'docs/reports/card_wb_name_plan_2026-10-15/label_full_text.json').read_text())
    for old in originals:
        matches = [r for r in rows if r['current'] == old['title']]
        if matches:
            r = matches[0]
            samples.append((r, old['title']))
    changes = json.loads((args.out / 'geometry_results.json').read_text())['changes']
    for change in changes:
        if change['new'] or not any(not r['proposed'] for r, _ in samples):
            r = next(r for r in rows if r['code'] == change['code'] and r['proposed'] == change['new'])
            if not any(a['ms_id'] == r['ms_id'] for a, _ in samples):
                samples.append((r, change['old']))
        if len(samples) >= 8:
            break
    pdf = PDF(args.out / 'NotoSans-Regular.ttf')
    for start in range(0, len(samples), 4):
        commands = [text(45, 812, 'Название WB: расчёт размещения по присланным PDF', 11),
                    text(45, 795, '58×40 мм; NotoSans 9 pt; консервативная строка 140 pt; максимум 2 строки.', 8),
                    text(45, 780, 'Исходный шаблон МС не получен. Это проверка геометрии, не производственная этикетка.', 7)]
        for j, (r, before) in enumerate(samples[start:start+4]):
            y = 601 - j * 177
            commands.extend([text(45, y + 139, f"{r['code']}: до / результат общей формулы", 9)])
            left = wrap_lines(before)
            commands.extend(label(45, y, before, r['code'], left is None or len(left) > 2))
            if r['status'] == 'HOLD':
                commands.extend(label(300, y, 'Не обновлять. Требуется разбор.', r['code']))
                commands.append(text(300, y - 10, 'HOLD: тип/источник или геометрия', 7))
            else:
                commands.extend(label(300, y, r['proposed'], r['code']))
                commands.append(text(300, y - 10, f"{len(r['proposed'])} знаков / {len(wrap_lines(r['proposed']))} строки", 7))
        pdf.page(commands)
    pdf.write(args.out / 'label_geometry_preview.pdf')
    print('Preview PDF:', len(samples), 'pairs;', len(pdf.pages), 'pages; no MS writes')


if __name__ == '__main__':
    main()

# поток: print
"""Build three local Diskver receipt design proposals without API calls.

Input: annotated MS receipt before A4 conversion. Output contains three A4
PDFs and a combined comparison. No deployment or print-style activation.
"""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from print_service.diskver_design import SELECTED_STYLE, STYLES, apply_diskver_design, qr_asset
from print_service.pdf import append_receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = args.source.read_bytes()
    combined = PdfWriter()
    manifest = {'source': str(args.source), 'variants': [],
                'qr': qr_asset()[1], 'selected_style': SELECTED_STYLE}
    for style, title in STYLES.items():
        modified = apply_diskver_design(source, style=style)
        writer = PdfWriter()
        append_receipt(writer, modified)
        output = args.output / ('diskver_' + style + '_A4.pdf')
        writer.add_metadata({'/Title': title})
        writer.write(str(output))
        output.chmod(0o600)
        combined.append(PdfReader(BytesIO(output.read_bytes())), import_outline=False)
        manifest['variants'].append({'style': style, 'title': title, 'file': output.name,
                                     'pages': len(writer.pages)})
    output = args.output / 'diskver_design_variants_A4.pdf'
    combined.add_metadata({'/Title': 'Дисквер — три варианта оформления товарного чека'})
    combined.write(str(output))
    output.chmod(0o600)
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'manifest.json').chmod(0o600)
    print('Created 3 design variants and combined A4 comparison:', args.output)


if __name__ == '__main__':
    main()

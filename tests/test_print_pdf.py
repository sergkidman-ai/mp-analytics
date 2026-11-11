# поток: print
from datetime import date
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

from print_service.domain import OrderPrintFiles, PrintBlocked
from print_service.pdf import A4, build_pair, extract_order_pair


def document(path, pages):
    writer = PdfWriter()
    for width, height, crop in pages:
        page = writer.add_blank_page(width=width, height=height)
        if crop:
            page.cropbox = RectangleObject(crop)
    writer.write(str(path))


class PrintPdfTests(unittest.TestCase):
    def test_single_order_is_extracted_from_published_pages_without_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);r1,r2,l1,l2=[root/name for name in ('r1.pdf','r2.pdf','l1.pdf','l2.pdf')]
            document(r1,[(612,792,None)]*2);document(r2,[(612,792,None)])
            document(l1,[(164.25,113.25,[1,2,163,112])]);document(l2,[(113,164,None)]*2)
            writer=PdfWriter();reader=PdfReader(l2)
            for page in reader.pages:writer.add_page(page).rotate(90)
            writer.write(l2)
            items=(OrderPrintFiles('oz_acc1','one','o1','d1',(str(r1),),(str(l1),)),
                   OrderPrintFiles('oz_acc1','two','o2','d2',(str(r2),),(str(l2),)))
            group=build_pair(items,shipment_date=date(2026,11,5),output_root=root/'groups')
            for path in (r1,r2,l1,l2):path.unlink()
            selected=extract_order_pair(group,'o2',output_root=root/'single')
            self.assertEqual(len(PdfReader(selected/'receipts.pdf').pages),1)
            pages=PdfReader(selected/'labels.pdf').pages
            self.assertEqual(len(pages),2)
            originals=PdfReader(group/'labels.pdf').pages[1:]
            for actual,original in zip(pages,originals):
                self.assertEqual(actual.mediabox,original.mediabox)
                self.assertEqual(actual.cropbox,original.cropbox)
                self.assertEqual(actual.rotation,original.rotation)
            before=(selected/'labels.pdf').read_bytes()
            self.assertEqual(selected,extract_order_pair(group,'o2',output_root=root/'single'))
            self.assertEqual(before,(selected/'labels.pdf').read_bytes())
            with self.assertRaises(PrintBlocked):extract_order_pair(group,'missing',output_root=root/'single')

    def test_native_labels_and_common_order_with_different_page_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r1, r2, l1, l2 = [root / name for name in ('r1.pdf', 'r2.pdf', 'l1.pdf', 'l2.pdf')]
            document(r1, [(612, 792, None), (612, 792, None)])
            document(r2, [(400, 600, None)])
            document(l1, [(164.25, 113.25, [1, 2, 163, 112])])
            document(l2, [(164.409, 113.386, None)] * 2)
            orders = (OrderPrintFiles('oz_acc1', 'first-1', 'o1', 'd1', (str(r1),), (str(l1),)),
                      OrderPrintFiles('oz_acc1', 'second-2', 'o2', 'd2', (str(r2),), (str(l2),)))
            out = build_pair(orders, shipment_date=date(2026, 11, 3), output_root=root / 'out')
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual([r['receipt_pages'] for r in manifest['orders']], [[1, 2], [3, 3]])
            self.assertEqual([r['label_pages'] for r in manifest['orders']], [[1, 1], [2, 3]])
            labels = PdfReader(out / 'labels.pdf')
            original = PdfReader(l1).pages[0]
            self.assertEqual(list(labels.pages[0].mediabox), list(original.mediabox))
            self.assertEqual(list(labels.pages[0].cropbox), list(original.cropbox))
            for page in PdfReader(out / 'receipts.pdf').pages:
                self.assertAlmostEqual(float(page.mediabox.width), A4[0], places=4)
                self.assertAlmostEqual(float(page.mediabox.height), A4[1], places=4)
            self.assertEqual(build_pair(orders, shipment_date=date(2026, 11, 3), output_root=root / 'out'), out)
            with ThreadPoolExecutor(max_workers=2) as pool:
                paths = list(pool.map(lambda _: build_pair(orders, shipment_date=date(2026, 11, 3), output_root=root / 'out'), range(2)))
            self.assertEqual(paths, [out, out])
            original_bytes = (out / 'labels.pdf').read_bytes()
            (out / 'labels.pdf').write_bytes(b'corrupt cached data')
            with self.assertRaises(PrintBlocked):
                build_pair(orders, shipment_date=date(2026, 11, 3), output_root=root / 'out')
            (out / 'labels.pdf').write_bytes(original_bytes)
            # A changed source must never return the previous cached pair.
            document(l2, [(165, 114, None)])
            other = build_pair(orders, shipment_date=date(2026, 11, 3), output_root=root / 'out')
            self.assertNotEqual(other, out)

    def test_missing_label_never_publishes_partial_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / 'receipt.pdf'
            document(receipt, [(612, 792, None)])
            order = OrderPrintFiles('wb_acc1', '123', 'o', 'd', (str(receipt),), (str(root / 'missing.pdf'),))
            with self.assertRaises(PrintBlocked):
                build_pair((order,), shipment_date=date(2026, 11, 3), output_root=root / 'out')
            self.assertEqual(list((root / 'out').glob('*/receipts.pdf')), [])

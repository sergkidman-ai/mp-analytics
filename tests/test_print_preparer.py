# поток: print
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pypdf import PdfWriter

from print_service.domain import PrintBlocked
from print_service.preparer import validate_native_label


class NativeLabelTests(unittest.TestCase):
    def test_rotated_native_yandex_label_is_accepted_without_mutation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'label.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=113, height=164).rotate(90)
            writer.write(path)
            before = path.read_bytes()
            validate_native_label(path)
            self.assertEqual(path.read_bytes(), before)

    def test_unexpected_size_is_rejected_without_resizing(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'label.pdf'
            writer = PdfWriter()
            writer.add_blank_page(width=595, height=842)
            writer.write(path)
            before = path.read_bytes()
            with self.assertRaises(PrintBlocked): validate_native_label(path)
            self.assertEqual(path.read_bytes(), before)

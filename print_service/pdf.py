# поток: print
"""Publish two PDFs from one fixed sequence. Label pages are never transformed."""
from contextlib import contextmanager
from datetime import date
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile

from pypdf import PageObject, PdfReader, PdfWriter, Transformation

from print_service.domain import PrintBlocked, freeze_batch

A4 = (595.2755905511812, 841.8897637795277)
PROFILE = 'receipts_a4_labels_native_v1'


def pdf_reader(data):
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not reader.pages:
            raise PrintBlocked('pdf_empty_or_encrypted')
        return reader
    except PrintBlocked:
        raise
    except Exception:
        raise PrintBlocked('invalid_pdf') from None


def append_receipt(writer, data):
    reader = pdf_reader(data)
    for source in reader.pages:
        source.transfer_rotation_to_content()
        box = source.cropbox
        width, height = float(box.width), float(box.height)
        if min(width, height) <= 0:
            raise PrintBlocked('invalid_receipt_page_size')
        scale = min(A4[0] / width, A4[1] / height)
        dx = (A4[0] - width * scale) / 2 - float(box.left) * scale
        dy = (A4[1] - height * scale) / 2 - float(box.bottom) * scale
        page = writer.add_blank_page(width=A4[0], height=A4[1])
        page.merge_transformed_page(source, Transformation().scale(scale).translate(dx, dy))
    return len(reader.pages)


def append_labels(writer, data):
    reader = pdf_reader(data)
    # Preserve MediaBox, CropBox, rotation and encoded vector/image content.
    # Do not apply A4 logic, resampling, fit-to-page, or text overlays here.
    writer.append(reader, import_outline=False)
    return len(reader.pages)


@contextmanager
def directory_lock(root):
    fd = os.open(root / '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def build_pair(orders, *, shipment_date: date, output_root):
    """Input receipt files must already contain signature/code/quantity/color edits.

    File contents, order and profile determine the cache key. Both PDFs are
    published by one atomic directory rename, so partial results stay private.
    The helper performs local file I/O only, with no API requests or exports.
    """
    plan = freeze_batch(orders, shipment_date=shipment_date)
    root = Path(output_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with directory_lock(root):
        artifacts = {}
        for order in orders:
            for path in order.receipt_files + order.label_files:
                if path not in artifacts:
                    try:
                        data = Path(path).read_bytes()
                    except OSError:
                        raise PrintBlocked('source_file_unavailable') from None
                    pdf_reader(data)
                    artifacts[path] = data
        plan['profile'] = PROFILE
        plan['source_sha256'] = {path: sha256(data).hexdigest() for path, data in artifacts.items()}
        key = sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        target = root / key
        if target.exists():
            manifest = json.loads((target / 'manifest.json').read_text())
            for name, digest in manifest['output_sha256'].items():
                if sha256((target / name).read_bytes()).hexdigest() != digest:
                    raise PrintBlocked('cached_pdf_checksum_mismatch')
            return target
        temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=root))
        try:
            receipts, labels = PdfWriter(), PdfWriter()
            for order, row in zip(orders, plan['orders']):
                starts = (len(receipts.pages) + 1, len(labels.pages) + 1)
                for path in order.receipt_files:
                    append_receipt(receipts, artifacts[path])
                for path in order.label_files:
                    append_labels(labels, artifacts[path])
                row['receipt_pages'] = [starts[0], len(receipts.pages)]
                row['label_pages'] = [starts[1], len(labels.pages)]
            plan['output_sha256'] = {}
            for name, writer in (('receipts.pdf', receipts), ('labels.pdf', labels)):
                path = temporary / name
                writer.write(str(path))
                path.chmod(0o600)
                plan['output_sha256'][name] = sha256(path.read_bytes()).hexdigest()
            path = temporary / 'manifest.json'
            path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
            path.chmod(0o600)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return target

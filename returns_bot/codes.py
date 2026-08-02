# поток: ret
"""Картинки штрихкодов.

Правило: рисуем ТОЛЬКО те коды, которые площадка отдала сама. Свой QR из номера
возврата не выдумываем — если ПВЗ сканирует другой код, картинка бесполезна
и вводит в заблуждение.

Источники:
    Ozon — `/v1/return/giveout/get-png` отдаёт готовый PNG (base64), его и шлём как есть;
           `logistic.barcode` в возврате — реальное значение, рисуем Code128;
    Яндекс — кода в API нет, картинок не будет (шлём инструкцию точки текстом).
"""
import base64
import io


def from_base64(png_b64):
    """PNG, который площадка прислала готовым."""
    if not png_b64:
        return None
    try:
        return base64.b64decode(png_b64)
    except Exception:
        return None


def code128(value):
    """Значение штрихкода → PNG с подписью. None, если значение пустое/нерисуемое."""
    if not value:
        return None
    try:
        import barcode
        from barcode.writer import ImageWriter
    except ImportError:
        return None
    try:
        buf = io.BytesIO()
        barcode.get("code128", str(value), writer=ImageWriter()).write(
            buf, options={"module_height": 12.0, "font_size": 8, "text_distance": 3.0})
        return buf.getvalue()
    except Exception:
        return None


def qr(value):
    """QR из значения, которое дала площадка (не из выдуманного номера)."""
    if not value:
        return None
    try:
        import qrcode
    except ImportError:
        return None
    try:
        img = qrcode.make(str(value))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None

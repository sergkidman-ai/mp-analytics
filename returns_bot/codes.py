# поток: ret
"""Картинки штрихкодов.

Правило: рисуем ТОЛЬКО те коды, которые площадка отдала сама. Свой QR из номера
возврата не выдумываем — если ПВЗ сканирует другой код, картинка бесполезна
и вводит в заблуждение.

Источники:
    Ozon — `/v1/return/giveout/get-png` отдаёт готовый PNG (base64); добавляем белые поля,
           не меняя пиксели штрихов и закодированное значение;
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


def for_scanning(png_bytes):
    """Белый фон и quiet zones вокруг готовых штрихов Ozon, без перерисовки кода.

    API отдаёт PNG, обрезанный до крайних штрихов (784×144 на обоих аккаунтах).
    Телеграм отправляет его файлом без пережатия, но на ПВЗ отсутствуют белые поля,
    необходимые сканеру для определения начала и конца Code128.
    """
    if not png_bytes:
        return None
    from itertools import groupby
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(png_bytes)) as source:
            if source.format != "PNG" or source.width * source.height > 10_000_000:
                return None
            rgba = source.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            rgb = white.convert("RGB")
        # Estimate the narrowest module on a scanline. Code128 needs >=10 modules
        # of white on each side; retain at least 80 pixels for the current API PNG.
        row = rgb.convert("L").crop((0, rgb.height // 2, rgb.width, rgb.height // 2 + 1)).tobytes()
        runs = [(dark, len(list(group))) for dark, group in groupby(p <= 127 for p in row)]
        if len(runs) < 10:
            return None
        module = min(length for _, length in runs[1:-1])
        framed = ImageOps.expand(rgb, border=(max(80, 10 * module), 48), fill="white")
        out = io.BytesIO()
        framed.save(out, format="PNG")
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
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

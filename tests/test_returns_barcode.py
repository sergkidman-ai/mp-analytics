# поток: ret
import base64
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from PIL import Image, ImageDraw
from returns_bot import bot, codes, daily_push, tg
from returns_bot.sources import ozon


def cropped_png(transparent=False):
    image = Image.new("RGBA", (210, 48), (255, 255, 255, 0 if transparent else 255))
    draw = ImageDraw.Draw(image)
    for x in range(0, image.width, 14):
        draw.rectangle((x, 0, x + 6, image.height - 1), fill=(0, 0, 0, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class BarcodeImageTests(unittest.TestCase):
    def test_white_quiet_zones_preserve_every_source_pixel(self):
        raw = cropped_png()
        original = Image.open(io.BytesIO(raw)).convert("RGB")
        framed = Image.open(io.BytesIO(codes.for_scanning(raw)))
        self.assertEqual(framed.mode, "RGB")
        x = (framed.width - original.width) // 2
        y = (framed.height - original.height) // 2
        self.assertGreaterEqual(x, 10 * 7)
        self.assertEqual(framed.crop((x, y, x + original.width, y + original.height)).tobytes(), original.tobytes())
        self.assertEqual(framed.crop((0, 0, x, framed.height)).getextrema(), ((255, 255),) * 3)
        self.assertEqual(framed.crop((framed.width - x, 0, framed.width, framed.height)).getextrema(), ((255, 255),) * 3)

    def test_transparent_gaps_are_white_on_dark_telegram_backgrounds(self):
        framed = Image.open(io.BytesIO(codes.for_scanning(cropped_png(True))))
        x = (framed.width - 210) // 2
        self.assertEqual(framed.mode, "RGB")
        self.assertEqual(framed.getpixel((x, 48)), (0, 0, 0))
        self.assertEqual(framed.getpixel((x + 10, 48)), (255, 255, 255))

    def test_invalid_or_blank_image_is_not_sent_as_a_barcode(self):
        self.assertIsNone(codes.for_scanning(b"not a png"))
        self.assertIsNone(codes.for_scanning(None))
        blank = io.BytesIO()
        Image.new("RGB", (200, 50), "white").save(blank, format="PNG")
        self.assertIsNone(codes.for_scanning(blank.getvalue()))


class BarcodeExpiryTests(unittest.TestCase):
    def test_live_pdf_utc_deadline_matches_application_moscow_time(self):
        with patch.object(ozon, "_pdf_text", return_value="Штрихкод действует до 24.10.2026 06:06:15"):
            till = ozon.giveout_valid_till(b"pdf")
        self.assertEqual(till, datetime(2026, 10, 24, 6, 6, 15, tzinfo=timezone.utc))
        self.assertEqual(till.astimezone(ozon.MSK).strftime("%d.%m %H:%M:%S"), "24.10 09:06:15")
        with patch.object(bot, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 23, 8, 20, tzinfo=timezone.utc)
            note = bot._till_note(till)
        self.assertIn("24.10 09:06 МСК", note)
        self.assertIn("осталось 21 ч", note)

    def test_utc_deadline_rolls_over_to_next_moscow_day(self):
        with patch.object(ozon, "_pdf_text", return_value="Штрихкод действует до 24.10.2026 23:59"):
            till = ozon.giveout_valid_till(b"pdf")
        self.assertEqual(till.astimezone(ozon.MSK).strftime("%d.%m %H:%M"), "25.10 02:59")

    def test_unknown_or_invalid_deadline_is_not_invented(self):
        for text in ("", "Штрихкод действует до 32.10.2026 06:06:15"):
            with self.subTest(text=text), patch.object(ozon, "_pdf_text", return_value=text):
                self.assertIsNone(ozon.giveout_valid_till(b"pdf"))


class BarcodeDeliveryTests(unittest.TestCase):
    def test_command_and_daily_summary_use_the_same_framed_file(self):
        source = cropped_png()
        g = {"value": "2347-5369", "png_b64": base64.b64encode(source).decode(),
             "till": datetime(2026, 10, 24, 6, 6, 15, tzinfo=timezone.utc)}
        with patch.object(ozon, "giveout", return_value=g), patch.object(bot, "datetime") as clock:
            clock.now.side_effect = lambda tz: datetime(2026, 10, 23, 12, tzinfo=timezone.utc).astimezone(tz)
            files = bot.barcode_files("oz_acc1")
            with patch.object(daily_push.render, "fetch", return_value=([{"account": "oz_acc1", "platform": "ozon"}], [])):
                daily_files = daily_push.barcode_photos()
        self.assertEqual(files, daily_files)
        name, png, caption, mime = files[0]
        self.assertEqual(mime, "image/png")
        self.assertEqual(name, "ozon-qr-tsifrovoy-2026-10-23.png")
        self.assertTrue(caption.startswith("Ozon - Цифровой\n"))
        self.assertGreater(Image.open(io.BytesIO(png)).width, 210)
        self.assertIn("24.10 09:06 МСК", caption)
        self.assertNotIn("2347-5369", caption)
        self.assertNotIn("Если не считывается", caption)
        with patch.object(tg, "_api") as api:
            tg.send_document("test", png, name, caption, mime=mime)
        args, kwargs = api.call_args
        self.assertEqual(args[0], "sendDocument")
        self.assertEqual(kwargs["files"]["document"][1], png)

    def test_broken_api_image_falls_back_to_its_exact_barcode_value(self):
        g = {"value": "2347-5369", "png_b64": base64.b64encode(b"not png").decode(), "till": None}
        with patch.object(ozon, "giveout", return_value=g), patch.object(codes, "code128", return_value=cropped_png()) as fallback:
            self.assertEqual(len(bot.barcode_files("oz_acc1")), 1)
        fallback.assert_called_once_with("2347-5369")


if __name__ == "__main__":
    unittest.main()

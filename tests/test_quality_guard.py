# поток: rev
"""Гвардия качества черновика (`reports/feedback_today._quality_guard`).

Главное обещание с 10.09.2026: guard ТЕКСТ НЕ ПРАВИТ. До этой даты он сам вписывал в ответ
«Да,»/«Нет,», и замер причин блоков (docs/reports/rev_block_reasons_2026-09-10.md) показал, что
это был собственный источник брака: «Нет, технически заправить можно…». Полярность знает автор
ответа; guard только помечает трейс, решение принимает модератор.

`./venv/bin/python -m unittest tests.test_quality_guard` — в БД и в сеть не ходит.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reports.feedback_today import _quality_guard      # noqa: E402

Q = "Можно ли заправлять этот картридж?"
HP1300 = "Технически заправить Q2613A можно, но гарантию мы на такие картриджи не даём."


class NoMutationTest(unittest.TestCase):
    def test_hp1300_q2613a_текст_не_тронут(self):
        out, viol = _quality_guard(HP1300, Q)
        self.assertEqual(out, HP1300)
        self.assertFalse(out.lower().startswith(("да,", "нет,")))
        self.assertTrue(any("не прямой ответ" in v for v in viol), viol)
        self.assertTrue(any("текст не изменён" in v for v in viol), viol)

    def test_полярность_нигде_не_дописывается(self):
        for reply in ("Заправка возможна, но ресурс упадёт.",
                      "К сожалению, этот картридж одноразовый.",
                      "Чип на плате, заправка технически возможна."):
            out, _ = _quality_guard(reply, Q)
            self.assertEqual(out, reply, reply)

    def test_прямой_ответ_проходит_без_пометки(self):
        out, viol = _quality_guard("Да, картридж заправляется, чип менять не нужно.", Q)
        self.assertEqual(out, "Да, картридж заправляется, чип менять не нужно.")
        self.assertFalse([v for v in viol if "не прямой ответ" in v], viol)


if __name__ == "__main__":
    unittest.main()

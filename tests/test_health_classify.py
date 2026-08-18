# поток: rev
"""Классификация причин сбоя провайдера (feedback_bot/health.py) — офлайн, без сети.

Повод: 18.08.2026 транзитный 529 Overloaded пришёл в Telegram как 🔴 «Ответы не уходят ·
Пополнить баланс», хотя баланс был полон. Цвет и подпись «что делать» должны следовать
из класса ошибки, а не быть одинаковыми на все случаи.
"""
import unittest

from feedback_bot.health import (classify, BILLING, AUTH, CONFIG, TRANSIENT, UNKNOWN)

OVERLOADED = ("LlmUnavailable: OverloadedError: Error code: 529 - {'type': 'error', 'error': "
              "{'type': 'overloaded_error', 'message': 'Overloaded'}, 're")


class TestClassify(unittest.TestCase):
    def test_529_транзитный_а_не_баланс(self):
        reason, cls = classify(OVERLOADED)
        self.assertEqual(cls, TRANSIENT)
        self.assertIn("529", reason)
        self.assertNotIn("БАЛАНС", reason)

    def test_пустой_баланс(self):
        for err in ("BadRequestError: Error code: 400 - your credit balance is too low",
                    "Error code: 402 - Insufficient Balance"):
            self.assertEqual(classify(err)[1], BILLING, err)

    def test_ключ(self):
        self.assertEqual(classify("AuthenticationError: Error code: 401 - invalid x-api-key")[1], AUTH)

    def test_модель(self):
        self.assertEqual(classify("NotFoundError: Error code: 404 - model: claude-opus-7")[1], CONFIG)

    def test_прочие_транзитные(self):
        for err in ("RateLimitError: Error code: 429 - rate limit exceeded",
                    "InternalServerError: Error code: 500 - internal server error",
                    "APIConnectionError: Connection error",
                    "ReadTimeout: timed out"):
            self.assertEqual(classify(err)[1], TRANSIENT, err)

    def test_неизвестное_не_транзитное(self):
        """Незнакомую ошибку нельзя молча считать бликом — она должна дойти до Сергея красным."""
        reason, cls = classify("ValueError: что-то новое")
        self.assertEqual(cls, UNKNOWN)
        self.assertIn("что-то новое", reason)

    def test_пусто(self):
        self.assertEqual(classify(None), ("неизвестная причина", UNKNOWN))


class TestHints(unittest.TestCase):
    def test_транзитный_не_зовёт_пополнять(self):
        from feedback_bot.health import _HINTS
        self.assertNotIn(TRANSIENT, _HINTS["anthropic"])
        self.assertNotIn(TRANSIENT, _HINTS["deepseek"])
        self.assertIn("console.anthropic.com", _HINTS["anthropic"][BILLING])


if __name__ == "__main__":
    unittest.main()

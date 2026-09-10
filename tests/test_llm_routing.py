# поток: rev
"""Роутинг вызовов модели и цена (пункты 1–3 задачи «Роутинг и стоимость LLM», 10.09.2026).

Смысл проверок: дорогая ветка должна включаться там, где ответ надо собрать, и НЕ включаться на
простом вопросе, у которого ответ буквально лежит в карточке. И обратное правило, более важное:
шаблон по карточке не имеет права родиться там, где нужного факта в карточке нет — это была бы
выдумка про товар, отправленная покупателю без человека.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reports import llm_routing as lr          # noqa: E402
from reports import llm_pricing as lp          # noqa: E402


class NeedsLlmTest(unittest.TestCase):
    def test_классы_сборки_идут_на_модель(self):
        for cls in ("претензия", "совместимость", "заправка", "инструкция"):
            need, why = lr.needs_llm("question", "текст обращения", cls)
            self.assertTrue(need, cls)
            self.assertIn(cls, why)

    def test_простой_вопрос_без_модели(self):
        need, _ = lr.needs_llm("question", "Чип есть?", "характеристики")
        self.assertFalse(need)

    def test_сравнение_идёт_на_модель(self):
        for q in ("Чем отличается от оригинального?", "Что лучше — этот или 728?",
                  "В чём разница между ними"):
            self.assertTrue(lr.needs_llm("question", q, "характеристики")[0], q)

    def test_многосоставный_идёт_на_модель(self):
        # два вопроса в одном обращении: шаблон ответит на первый и промолчит о втором
        self.assertTrue(lr.is_multi("Чип стоит? И ещё — на сколько страниц хватит?"))
        self.assertTrue(lr.is_multi("Ресурс какой? Заправлять можно?"))
        self.assertTrue(lr.needs_llm("question", "Чип стоит? Ресурс какой?", "характеристики")[0])

    def test_отзыв_по_длине_текста(self):
        short = "Спасибо!"
        long = "Картридж пришёл быстро, установили в принтер, печатает ровно, качеством довольны вполне"
        self.assertFalse(lr.needs_llm("review", short, None, 5)[0])
        self.assertTrue(lr.needs_llm("review", long, None, 5)[0])
        self.assertGreater(len(long), lr.REVIEW_TEXT_MIN)


class CardAnswerTest(unittest.TestCase):
    def test_чип_установлен(self):
        txt, mark = lr.card_answer("характеристики", "Чип установлен?", {"chip": "installed"})
        self.assertIn("чип установлен", txt.lower())
        self.assertIn("чип", mark)

    def test_чип_отсутствует_с_ссылкой_на_пару(self):
        txt, _ = lr.card_answer("характеристики", "Чип есть?", {"chip": "none"},
                                chip_line="Версия с чипом — вот здесь: ссылка")
        self.assertIn("без чипа", txt)
        self.assertIn("Версия с чипом", txt)

    def test_нет_факта_в_карточке_нет_ответа(self):
        # главное правило: неизвестный чип НЕ превращается ни в «да», ни в «нет»
        self.assertEqual(lr.card_answer("характеристики", "Чип есть?", {"chip": None}), (None, None))
        self.assertEqual(lr.card_answer("ресурс", "Какой ресурс?", {}), (None, None))
        self.assertEqual(lr.card_answer("комплектация", "Что в комплекте?", {}), (None, None))

    def test_наличие_шаблоном_не_отвечаем(self):
        # остатка в фактах карточки нет, а «да, в наличии» — обещание отгрузки
        self.assertEqual(lr.card_answer("наличие", "Есть в наличии?", {"chip": "installed"}),
                         (None, None))

    def test_ресурс_и_комплектация_по_фактам(self):
        txt, _ = lr.card_answer("ресурс", "На сколько страниц хватит?", {"resource": "2300"})
        self.assertIn("2300", txt)
        txt2, _ = lr.card_answer("комплектация", "Что входит в набор?",
                                 {"set_info": "4 картриджа: чёрный, голубой, пурпурный, жёлтый"})
        self.assertIn("4 картриджа", txt2)

    def test_многосоставный_шаблоном_не_закрывается(self):
        self.assertEqual(
            lr.card_answer("характеристики", "Чип есть? И заправлять можно?", {"chip": "installed"}),
            (None, None))


class VerifyTest(unittest.TestCase):
    """Контролёр разбирает ответ модели и НИКОГДА не рушит готовый черновик."""

    class _Msg:
        def __init__(self, text):
            self.content = [type("B", (), {"type": "text", "text": text})()]
            self.usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()

    def _create(self, text):
        return lambda **kw: self._Msg(text)

    def test_pass(self):
        v, note, _ = lr.verify(self._create('{"verdict":"PASS","note":""}'), "m", "question",
                               "Чип есть?", "Да, чип установлен.", "ФАКТЫ: чип установлен")
        self.assertEqual(v, "PASS")

    def test_fix_с_причиной(self):
        v, note, _ = lr.verify(self._create('{"verdict":"FIX","note":"ресурс не из карточки"}'),
                               "m", "question", "Ресурс?", "10000 страниц", "ФАКТЫ: —")
        self.assertEqual(v, "FIX")
        self.assertIn("ресурс", note)

    def test_мусор_вместо_json_не_валит_черновик(self):
        for bad in ("извините, не могу", "{битый json"):
            v, _, _ = lr.verify(self._create(bad), "m", "question", "?", "ответ", "данные")
            self.assertEqual(v, "PASS", bad)


class PricingTest(unittest.TestCase):
    def test_батч_вдвое_дешевле(self):
        full = lp.cost("claude-sonnet-4-6", 1_000_000, 100_000)
        half = lp.cost("claude-sonnet-4-6" + lp.BATCH_SUFFIX, 1_000_000, 100_000)
        self.assertAlmostEqual(full, 3.00 + 1.50, places=6)
        self.assertAlmostEqual(half, full / 2, places=6)

    def test_sonnet_дешевле_opus(self):
        self.assertLess(lp.cost("claude-sonnet-4-6", 1_000_000, 100_000),
                        lp.cost("claude-opus-5", 1_000_000, 100_000))

    def test_неизвестная_модель_не_придумывает_цену(self):
        self.assertEqual(lp.cost("deepseek-v4-pro", 1_000_000, 1_000_000), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# поток: rev
"""Недельный дайджест контентщику и закупщику (reports/feedback_digest.py, 11.09.2026).

Проверяем ровно то, от чего зависит доверие к спискам. Дайджест никому не отвечает, поэтому
техническая цена ошибки нулевая — а вот цена мусора в списке высокая: закупщик, один раз
получивший артикул «за компанию», второй список уже не откроет.

Три вещи под контролем:
  • тема и симптом определяются по словам покупателя, а не «примерно»;
  • претензия «после заправки» не попадает в список закупщика (прямое указание Сергея);
  • кандидат в заголовок не тащит за собой куски русских слов («На 3103fdw», «ртридж 106R…»).

Тест офлайн: в БД не ходит.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reports import feedback_digest as fd                 # noqa: E402


class TopicTest(unittest.TestCase):
    def t(self, text):
        return fd._first(fd.TOPICS, text)

    def test_темы_контентщика(self):
        self.assertEqual(self.t("К картриджу для работы нужен чип?"), "чип")
        self.assertEqual(self.t("Подойдёт для M236sdn?"), "совместимость")
        self.assertEqual(self.t("Сколько картриджей в наборе?"), "комплектация")
        self.assertEqual(self.t("Его можно заправлять?"), "заправка")
        self.assertEqual(self.t("Это оригинал или подделка?"), "оригинальность")
        self.assertEqual(self.t("Чернила пигментные или водорастворимые?"), "тип чернил")

    def test_вопрос_поддержки_темы_не_имеет(self):
        # «Когда мне ответят?» и «какая доставка» — работа поддержки, а не дырка в карточке
        self.assertIsNone(self.t("Как долго мне ещё ждать?"))
        self.assertIsNone(self.t("Какая доставка?"))


class SymptomTest(unittest.TestCase):
    def s(self, text):
        return fd._first(fd.SYMPTOMS, text)

    def test_симптомы_закупщика(self):
        self.assertEqual(self.s("Принтер не видит картридж"), "не видит")
        self.assertEqual(self.s("Печатает с полосами, полосит по всему листу"), "полосит")
        self.assertEqual(self.s("Тонер мажет и пачкает лист"), "мажет")
        self.assertEqual(self.s("Картридж пришёл пустой"), "пустой")
        self.assertEqual(self.s("Не подошёл к принтеру"), "не подошёл")
        self.assertEqual(self.s("Постоянно зажёвывает бумагу"), "сбой подачи")
        self.assertEqual(self.s("Пишет ошибку картриджа"), "ошибка чипа")

    def test_похвала_симптомом_не_считается(self):
        self.assertIsNone(self.s("Отличный картридж, всё отлично"))


class RefillTest(unittest.TestCase):
    def test_после_заправки_отсекается(self):
        self.assertTrue(fd.REFILLED_RX.search("После заправки перестал печатать"))
        self.assertTrue(fd.REFILLED_RX.search("Сам заправил, полосит"))

    def test_вопрос_о_заправке_не_отсекается(self):
        # иначе из списка закупщика вылетел бы любой картридж со словом «заправка» в отзыве
        self.assertFalse(fd.REFILLED_RX.search("Скажите, его можно заправлять?"))


class MismatchTest(unittest.TestCase):
    def test_карточка_обещала_не_то(self):
        self.assertTrue(fd.MISMATCH_RX.search("в карточке товара одно фото, по факту совсем другой товар"))
        self.assertTrue(fd.MISMATCH_RX.search("Пришёл не как в описании"))

    def test_обычная_претензия_сюда_не_попадает(self):
        self.assertFalse(fd.MISMATCH_RX.search("Картридж полосит с первого листа"))


class ModelTest(unittest.TestCase):
    def models(self, q):
        return [m.group(0) for m in fd.MODEL_RX.finditer(q)]

    def test_кириллица_не_прилипает(self):
        self.assertEqual(self.models("На 3103fdw подойдет?"), ["3103fdw"])
        self.assertIn("106R01158", self.models("нужен картридж 106R01158. Это он и есть?"))
        self.assertNotIn("ртридж 106R01158", self.models("нужен картридж 106R01158"))

    def test_голое_число_моделью_не_считается(self):
        # «Картридж 412 и 512 одного размера?» — это коды картриджей, не модели принтера
        self.assertEqual(self.models("Картридж 412 и 512 одного размера?"), [])


class RenderTest(unittest.TestCase):
    def data(self, **kw):
        d = {"gaps": [], "mismatch": [], "titles": [], "titles_note": None,
             "symptoms": [], "skipped_refill": 3, "colors": []}
        d.update(kw)
        return d

    def test_шапка_называет_число_отсеянных(self):
        md = fd.render_purchase(self.data(), 7, "2026-09-11")
        self.assertIn("после заправки", md)
        self.assertIn("**3**", md)

    def test_цвет_показан_долей_а_не_только_числом(self):
        md = fd.render_purchase(self.data(colors=[
            {"color": "мажента", "n": 4, "base": 6, "articles": ["0960", "5243"],
             "texts": [{"article": "0960", "rating": 2, "text": "пурпурный пустой"}]}]),
            7, "2026-09-11")
        self.assertIn("4 из 6", md)
        self.assertIn("67 %", md)

    def test_пустые_списки_говорят_об_этом_прямо(self):
        # молчащий файл должен читаться как «за окно таких артикулов нет», а не как сбой сборки
        self.assertIn("нет", fd.render_purchase(self.data(), 7, "2026-09-11"))
        self.assertIn("нет", fd.render_content(self.data(), 7, "2026-09-11"))

    def test_сообщение_в_телеграм_считает_обоих_адресатов(self):
        import pathlib
        d = self.data(gaps=[{"article": "3815", "topic": "чип", "n": 2, "product": "",
                             "texts": [], "advice": "x"}],
                      symptoms=[{"article": "0035", "symptom": "не видит", "n": 2, "product": "",
                                 "stars": [1], "texts": []}])
        d.update(day="2026-09-11", days=7)
        txt = fd.tg_text(d, (pathlib.Path("/a/content.md"), pathlib.Path("/a/purchasing.md")))
        self.assertIn("Контентщику: <b>1</b>", txt)
        self.assertIn("Закупщику: <b>1</b>", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)

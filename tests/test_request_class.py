# поток: rev
"""Классы обращения и правило A1.1 «претензия в форме вопроса» (08.09.2026).

Кейсы взяты из docs/reports/rev_other_class_2026-09-08.md — выгрузки 39 вопросов, которые
классификатор до правки сваливал в «прочее», и из двух строк Xerox C310, ради которых заводился
A1.1-fix. Офлайн: ни БД, ни сети.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reports import request_class as rc          # noqa: E402
from reports import answer_cache as ac           # noqa: E402


def q(text):
    return rc.classify(text, kind="question")


class NewClassesTest(unittest.TestCase):
    def test_manufacturer(self):
        for t in ["Добрый день! подскажите пожалуйста производителя данных картриджей. спасибо.",
                  "добрый вечер. подскажите кто производитель чернил?",
                  "Какая фирма производитель?",
                  "Будет ли товарный знак ?"]:
            self.assertEqual(q(t), "производитель", t)

    def test_dimensions(self):
        for t in ["Длина шнура", "Здравствуйте,какой размер?",
                  "Картридж 412 черный и картридж 512 одного размера?",
                  "Картридж 412 и 512 одного размера?"]:
            self.assertEqual(q(t), "габарит", t)

    def test_set_contents(self):
        for t in ["В комплекте 10 шт. какие картриджи?",
                  "Сколько картриджей в наборе?",
                  "Здравствуйте! В этом комплекте 4 шт., как на картинке, или 2 шт.?"]:
            self.assertEqual(q(t), "комплектация", t)

    def test_new_classes_are_cacheable(self):
        # источник ответа — атрибуты карточки, ответ по артикулу один: ключ = константа класса
        for cls in ("производитель", "комплектация"):
            self.assertIn(cls, ac.KEYABLE)
            key, why = ac.question_key("любой текст вопроса", cls)
            self.assertEqual(key, cls, why)
            self.assertTrue(ac.SIG_FIELDS.get(cls), f"нет полей отпечатка для «{cls}»")

    def test_dimension_key_splits_by_measurement(self):
        # ревью Codex 08.09.2026: у габарита ключ — спрошенное измерение, иначе ответ про длину
        # подставится на вопрос про диаметр. Отпечаток карточки обязан быть у каждого ключа.
        self.assertIn("габарит", ac.KEYABLE)
        keys = {ac.question_key(t, "габарит")[0]
                for t in ("Какая длина шнура?", "Какой диаметр?", "Какой размер?")}
        self.assertEqual(len(keys), 3, keys)
        for k in keys:
            self.assertTrue(ac.card_signature({"weight_pkg": 1, "name": "x"}, k), k)
        self.assertIsNone(ac.question_key("любой текст вопроса", "габарит")[0])

    def test_warranty_question_is_cacheable(self):
        # п.4 брифа: «какая гарантия» — свойство товара из карточки, а не обращение к человеку
        cls = q("Какая гарантия на картридж?")
        self.assertEqual(cls, "характеристики")
        key, why = ac.question_key("Какая гарантия на картридж?", cls)
        self.assertEqual(key, "атрибут:гарантия", why)
        self.assertTrue(ac.card_signature({"kind": "совместимый", "name": "x"}, key))


class WidenedRulesTest(unittest.TestCase):
    def test_compatibility_implicit(self):
        for t in ["Это же аналог картриджа HP-45 (51645A), верно?",
                  "Здравствуйте! Для моего Xerox WorkCentre 3119 нужен картридж 106R01158. Это он и есть?",
                  "Скажите, данный картридж может использоваться вместо картриджа Star SP500?",
                  "Есть ли у Вас картриджи для принтера ОКИ c 612 dn?",
                  "Обьясните ,как он может подойти к Epson Offiice TX 510 fn?"]:
            self.assertEqual(q(t), "совместимость", t)

    def test_refill(self):
        for t in ["когда закончится краска можно ли в нее залить краску?",
                  "Можно ли туда ложить чернилы?",
                  "Он одноразовый или перезаряжаемый?"]:
            self.assertEqual(q(t), "заправка", t)

    def test_ink_type_goes_to_specs(self):
        for t in ["Здравствуйте. Черные чернила пигментные?",
                  "Красители водорастворимые или пигментные?",
                  "Добрый день. В картридже пигментные или водорасворимые чернила?",
                  "Добрый день. Картриджи с тонером (краской)?",
                  "Здравствуйте! драм новый или ref?"]:
            self.assertEqual(q(t), "характеристики", t)

    def test_warranty_as_property(self):
        # срок гарантии — свойство товара из карточки; поломка «по гарантии» уходит в претензию
        self.assertEqual(q("Какая гарантия на картриджи?"), "характеристики")
        self.assertEqual(q("Сколько гарантия?"), "характеристики")
        self.assertEqual(q("Картридж сломался, хочу по гарантии"), "претензия")


class ClaimOverrideTest(unittest.TestCase):
    """A1.1: переопределение ПОСЛЕ основной классификации."""

    def test_set_question_stays_set_question(self):
        self.assertEqual(q("Сколько картриджей в комплекте?"), "комплектация")

    def test_delivered_mismatch_is_claim(self):
        self.assertEqual(
            q("Сколько картриджей в комплекте? В описании смук 4шт. Приехал один чёрный."),
            "претензия")

    def test_xerox_c310_pair(self):
        # обе строки классификатор относил к «ресурсу» и «характеристикам» — по теме верно,
        # по существу это претензии, и держал их только стоп-лист A1.2
        self.assertEqual(q("Подскажите, купила картриджи в школьный принтер. Распечатала около "
                           "100 цветных страниц и пурпурный картридж оказался пустым? "
                           "Как такое возможно?"), "претензия")
        self.assertEqual(q("Как такое возможно, все картриджи полные, а красный пустой. "
                           "А распечатано было около 100 разноцветных листов ? Вы серьезно?"),
                         "претензия")

    def test_purchase_alone_is_not_a_claim(self):
        # покупка без описанного отказа — обычный вопрос, на человека его гнать не за что
        self.assertEqual(q("Купил ваш картридж, подойдёт ли он к HP LaserJet 1320?"),
                         "совместимость")
        self.assertEqual(q("Заказал набор, сколько картриджей в комплекте?"), "комплектация")

    def test_hard_markers_still_win(self):
        self.assertEqual(q("Картридж не печатает, что делать?"), "претензия")
        self.assertEqual(rc.classify("так себе", kind="review", rating=2), "претензия")

    def test_codex_false_positives(self):
        # ревью Codex 08.09.2026: повелительное «пришлите» и настоящее время «приходит»
        # не означают состоявшуюся покупку — претензии из них быть не должно
        self.assertFalse(rc.is_claim_text("Пришлите фото товара, как в описании"))
        self.assertEqual(q("На фото видна упаковка: приходит ли она в комплекте?"), "комплектация")

    def test_codex_false_negatives(self):
        # и наоборот: отглагольное «после установки» и «не соответствует» — это претензии
        self.assertTrue(rc.is_claim_text("После установки картридж печатает пустые листы"))
        self.assertTrue(rc.is_claim_text("Получил товар, цвет не соответствует фото"))
        self.assertTrue(rc.is_claim_text(
            "Второй раз мне уже приходит набор картриджей, не такой как на фото"))

    def test_codex_rule_order(self):
        self.assertEqual(q("Сколько мл чернил заливать при заправке?"), "заправка")
        self.assertEqual(q("Как поместить картридж в принтер?"), "инструкция")

    def test_is_claim_text_is_public(self):
        self.assertTrue(rc.is_claim_text("пришёл не тот картридж"))
        self.assertFalse(rc.is_claim_text("подойдёт ли к HP 1320?"))


class NegationTest(unittest.TestCase):
    """Отрицание перед маркером дефекта (10.09.2026, отчёт rev_block_reasons).

    Три ложных «претензии» — отзывы 5★ Яндекса, где CLAIM_RX ловил подстроку в похвале.
    """

    def test_похвала_с_отрицанием_не_претензия(self):
        for t in ("Не полосит, печатает чётко", "Печатает отлично, не мажет",
                  "Без полос, качество супер", "Нет ошибок, принтер увидел сразу",
                  "Никаких дефектов за месяц"):
            self.assertFalse(rc.is_claim_text(t, kind="review", rating=5), t)

    def test_настоящая_жалоба_держится(self):
        for t in ("Печатал чу-чуть размылено, полосит по краю",
                  "Пришёл с дефектом корпуса", "Мажет на каждой странице"):
            self.assertTrue(rc.is_claim_text(t, kind="review", rating=5), t)

    def test_отрицание_у_маркера_действия_не_гасится(self):
        # «деньги не вернули», «замену не сделали» — отрицание тут УСИЛИВАЕТ претензию
        self.assertTrue(rc.is_claim_text("Деньги так и не вернули"))
        self.assertTrue(rc.is_claim_text("Замену не сделали до сих пор"))


class NoTextTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(rc.classify("", kind="question"), "прочее")
        self.assertEqual(rc.classify("", kind="review", rating=5), "оценка без текста")


if __name__ == "__main__":
    unittest.main()

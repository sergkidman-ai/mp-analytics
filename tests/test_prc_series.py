# поток: prc
"""Серия карточки для инфографики ТК (prices/series.py).

Матрица пришла файлом и правится редко, поэтому проверяем не её содержимое, а разбор
формул: пороги стоят на границах («до 5000» — это 5000 включительно), трёхступенчатая
ячейка отдаёт среднюю серию между порогами, а неизвестная пара молчит, а не гадает.
Написание серии — как в справочнике МоегоСклада («Про»), иначе значение не найдётся.
"""
import unittest

from prices import series


class Formula(unittest.TestCase):
    def test_порог_включительно_в_младшую(self):
        for price, want in ((4999, "Бизнес"), (5000, "Бизнес"), (5001, "ПРО")):
            with self.subTest(price=price):
                self.assertEqual(series.series("Картридж", "для лазерного принтера", price)[0], want)

    def test_три_ступени(self):
        f = lambda p: series.series("Картридж", "для струйного принтера", p)[0]
        self.assertEqual([f(600), f(601), f(3999), f(4000)], ["Комфорт", "Бизнес", "Бизнес", "ПРО"])

    def test_серия_без_порога_не_требует_цены(self):
        self.assertEqual(series.series("Девелопер", "для лазерного принтера", "")[0], "ПРО")

    def test_порог_без_цены_молчит(self):
        value, why = series.series("Картридж", "для лазерного принтера", "")
        self.assertIsNone(value)
        self.assertIn("цена", why)

    def test_пары_нет_в_матрице(self):
        self.assertIsNone(series.series("Лента для принтера", "для лазерного принтера", 100)[0])
        self.assertIsNone(series.series("Фотобумага", "для струйного принтера", 100)[0])

    def test_написание_как_в_файле_матрицы(self):
        # Пишем «ПРО» (решение Сергея 18.09.2026); со справочником МС, где значение
        # называется «Про», их мирит поиск без учёта регистра в `ms_import`.
        self.assertEqual(series.SERIES["Про"], "ПРО")
        self.assertEqual(set(series.SERIES.values()), {"Комфорт", "Бизнес", "ПРО"})

    def test_вне_матрицы_серии_нет(self):
        # Такой товар (фотобумага, лампы, кассеты) мы не заводим вовсе — строка уходит в ЧС,
        # а карточку без серии обработчик `ms-create` не создаёт.
        for kind in ("Фотобумага", "Лампа", ""):
            with self.subTest(kind=kind):
                self.assertIsNone(series.series(kind, "для струйного принтера", 500)[0])

    def test_список_типов_для_формы(self):
        self.assertEqual(len(series.kinds()), 21)
        self.assertIn("Картридж", series.kinds())
        self.assertNotIn("", series.kinds())


class Printer(unittest.TestCase):
    def test_по_типу_расходника(self):
        self.assertEqual(series.guess_printer("что угодно", "Чернила"), "для струйного принтера")
        self.assertEqual(series.guess_printer("что угодно", "Лента для принтера"),
                         "для матричного принтера")

    def test_по_слову_в_названии(self):
        self.assertEqual(series.guess_printer("Картридж струйный Epson"), "для струйного принтера")
        self.assertEqual(series.guess_printer("Картридж для факса Panasonic"), "для факса")

    def test_по_умолчанию_лазерный(self):
        self.assertEqual(series.guess_printer("Картридж HP CF230X"), "для лазерного принтера")

    def test_все_догадки_есть_в_списке(self):
        for name, kind in (("Чернила Epson", ""), ("Риббон", ""), ("Картридж HP", "")):
            self.assertIn(series.guess_printer(name, kind), series.PRINTERS)


if __name__ == "__main__":
    unittest.main()

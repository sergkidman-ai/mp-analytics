# поток: prc
"""Правило «повреждённая упаковка» (prices/blacklist.in_damaged).

Правило работает по наименованию и на ВСЕХ поставщиков сразу, поэтому цена ошибки
несимметрична: пропустить метку — вернуть человеку строку, которую он и так забракует,
а поймать лишнее — молча потерять нормальный товар. Проверяем обе стороны: живые формы
метки из прайсов ВТТ и слова, внутри которых «пу» меткой не является.
"""
import unittest

from prices import blacklist


DAMAGED = [
    "Картридж Hi-Black для Olivetti PR II, ПУ",
    "Картридж HP LJ P1005/P1006 (NetProduct) NEW CB435A, 1,5K ПУ",
    "Картридж NetProduct (N-W1510X) для HP LJ Pro 4003dw, 9,7K П/У",
    "Картридж NetProduct (N-CC533A) для HP CLJ CP2025, M, 2,8K (П/У)",
    "Тонер-картридж NetProduct (N-Type 1270D) для Ricoh Aficio 1515, 7K (Повр. упак.)",
    "Картридж NetProduct (N-CF281X) для HP LJ Enterprise M630z, 25K (Поврежд. упак.)",
    "Картридж NetProduct (N-Q2612A) для HP LJ 1010/1020/3050, 2K (Повреждённая упаковка)",
]

CLEAN = [
    "Картридж лазерный Cactus CS-TK3405 TK-3405 чёрный",
    "Тонер-картридж Hi-Black (HB-TK-5390Bk) для Kyocera",
    "Драм-картридж Konica Minolta bizhub 3602P/4702",
    "Блок проявки DV-313K для Konica Minolta bizhub",     # «пу» внутри слова «проявки» нет,
    "Картридж для лазерного принтера, пурпурный",          # а вот «пурпурный» — есть
    "Заправочный комплект SP PC-211EV",
]


class DamagedPack(unittest.TestCase):
    def test_метка_ловится(self):
        for name in DAMAGED:
            with self.subTest(name=name):
                self.assertIsNotNone(blacklist.in_damaged(name))

    def test_нормальный_товар_не_трогаем(self):
        for name in CLEAN:
            with self.subTest(name=name):
                self.assertIsNone(blacklist.in_damaged(name))

    def test_пустое_имя(self):
        self.assertIsNone(blacklist.in_damaged(None))
        self.assertIsNone(blacklist.in_damaged(""))

    def test_mark_ставит_blacklisted(self):
        """Строка новинки с меткой получает причину `blacklisted` и не едет в разбор."""
        rows = [{"article": "991531325p", "name": DAMAGED[1], "reason": "not_found"},
                {"article": "CS-TK3405", "name": CLEAN[0], "reason": "not_found"}]
        out, hits = blacklist.mark(rows, black=set())
        self.assertEqual(hits, 1)
        self.assertEqual(out[0]["reason"], "blacklisted")
        self.assertEqual(out[1]["reason"], "not_found")


if __name__ == "__main__":
    unittest.main()

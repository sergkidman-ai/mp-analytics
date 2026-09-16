# поток: prc
"""Ключ поставщика карточки для добора в оприходование (prices/enter_add.key_of_supplier).

Ошибка здесь пишет остаток в ЧУЖУЮ партию, поэтому проверяем именно отказ: контрагент,
который сидит в группах двух поставщиков, ключа не получает. Группы подменяем — сеть и МС
тесту не нужны.
"""
import unittest
from unittest import mock

from prices import enter_add


class SupplierKey(unittest.TestCase):
    def setUp(self):
        enter_add._KEY_BY_SUPPLIER = None

    def tearDown(self):
        enter_add._KEY_BY_SUPPLIER = None

    def _groups(self, groups):
        return (mock.patch.object(enter_add, "PROC", list(groups)),
                mock.patch.object(enter_add, "get_identity", side_effect=lambda k: k),
                mock.patch.object(enter_add, "own_ids", side_effect=lambda k: groups[k]))

    def test_свой_контрагент(self):
        p = self._groups({"vtt": {"kpd", "vtt-neva"}, "bulat": {"tonerstor"}})
        with p[0], p[1], p[2]:
            self.assertEqual(enter_add.key_of_supplier("vtt-neva"), "vtt")
            self.assertEqual(enter_add.key_of_supplier("tonerstor"), "bulat")

    def test_контрагент_в_двух_группах_не_угадываем(self):
        p = self._groups({"vtt": {"shared", "kpd"}, "rapid": {"shared"}})
        with p[0], p[1], p[2]:
            self.assertIsNone(enter_add.key_of_supplier("shared"))
            self.assertEqual(enter_add.key_of_supplier("kpd"), "vtt")

    def test_чужой_контрагент(self):
        p = self._groups({"vtt": {"kpd"}})
        with p[0], p[1], p[2]:
            self.assertIsNone(enter_add.key_of_supplier("someone"))

    def test_api_поставщики_в_доборе(self):
        for key in ("bulat", "profiline", "vtt", "rapid", "easy_print", "ramis"):
            self.assertIn(key, enter_add.PROC)


if __name__ == "__main__":
    unittest.main()

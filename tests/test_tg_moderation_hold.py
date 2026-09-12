# поток: rev
"""Блок гейта перед самой отправкой не должен хоронить карточку модерации.

Инцидент 12.09.2026 (отзыв 067H, mod=650): _do_send при запрещающем вердикте ставил
state='queued' с СОХРАНЁННЫМ tg_msg_id и затирал сообщение плашкой без кнопок. Строка
выпадала из _pending (там tg_msg_id IS NULL) и не показывалась больше никогда.
"""
import sys
import pathlib
import unittest

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from feedback_bot import tg_moderation as tm          # noqa: E402
from reports import publish_gate                      # noqa: E402


class HoldCardTest(unittest.TestCase):
    def setUp(self):
        self.sets = []
        self.edits = []
        self._set, self._card, self._edit = tm._set, tm._card, tm.edit_text
        tm._set = lambda mod_id, state, **f: self.sets.append((mod_id, state, f))
        tm._card = lambda row: "КАРТОЧКА"
        tm.edit_text = lambda c, m, t, reply_markup=None: self.edits.append((c, m, t, reply_markup))

    def tearDown(self):
        tm._set, tm._card, tm.edit_text = self._set, self._card, self._edit

    def test_карточка_остаётся_показанной_и_с_кнопками(self):
        tm._hold_card(650, {"platform": "wb"}, 111, 757,
                      ["претензия, ручной ответ"], "⛔ нельзя")
        mod_id, state, fields = self.sets[0]
        self.assertEqual((mod_id, state), (650, "carded"))
        self.assertNotIn("tg_msg_id", fields)          # карточку в чате не теряем
        self.assertIn("претензия", fields["error"])
        _c, _m, text, kb = self.edits[0]
        self.assertIn("КАРТОЧКА", text)                # черновик остаётся материалом для ответа
        self.assertTrue(kb and kb["inline_keyboard"])  # кнопки вернулись

    def test_без_сообщения_в_чате_строка_возвращается_в_очередь(self):
        tm._hold_card(650, {"platform": "wb"}, None, None,
                      ["претензия, ручной ответ"], "⛔ нельзя")
        mod_id, state, fields = self.sets[0]
        self.assertEqual((mod_id, state), (650, "queued"))
        self.assertIsNone(fields["tg_msg_id"])         # иначе не попадёт в _pending
        self.assertFalse(self.edits)

    def test_претензия_кнопкой_не_обходится(self):
        self.assertFalse(publish_gate.can_override(["претензия, ручной ответ"]))


class PendingSqlTest(unittest.TestCase):
    def test_запрос_берёт_вернувшиеся_в_очередь_карточки(self):
        import inspect
        src = inspect.getsource(tm._pending)
        self.assertIn("m.carded_at < now() - interval '2 hours'", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)

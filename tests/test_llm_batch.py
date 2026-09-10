# поток: rev
"""Очередь батча: ключ, дедупликация, ручной режим (пункт 2 задачи 10.09.2026).

Проверяем ровно то, за что платят деньги: один и тот же промпт не уезжает в батч дважды, готовый
ответ подставляется без нового вызова, а внутри `synchronous()` очередь выключена — иначе ручная
перегенерация в боте вместо ответа отдавала бы оператору «ждите час».

Тест ходит в БД (таблицы миграции 706) и за собой прибирает: ключи с префиксом теста.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db                              # noqa: E402
from reports import llm_batch as lb              # noqa: E402

R = {"platform": "wb", "account": "wb_acc1", "kind": "question", "ext_id": "test-llm-batch-1"}


def _cleanup():
    db.execute("DELETE FROM feedback_llm_queue WHERE ext_id=%s", (R["ext_id"],))
    db.execute("""DELETE FROM feedback_llm_result WHERE req_key IN
                  (SELECT req_key FROM feedback_llm_queue WHERE ext_id=%s)""", (R["ext_id"],))


class QueueTest(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.k = lb._key("claude-sonnet-4-6", "draft", R, "СОДЕРЖИМОЕ ПРОМПТА")

    def tearDown(self):
        db.execute("DELETE FROM feedback_llm_result WHERE req_key=%s", (self.k,))
        _cleanup()

    def test_ключ_стабилен_и_зависит_от_промпта(self):
        self.assertEqual(self.k, lb._key("claude-sonnet-4-6", "draft", R, "СОДЕРЖИМОЕ ПРОМПТА"))
        self.assertNotEqual(self.k, lb._key("claude-sonnet-4-6", "draft", R, "ДРУГОЙ ПРОМПТ"))
        self.assertNotEqual(self.k, lb._key("claude-opus-5", "draft", R, "СОДЕРЖИМОЕ ПРОМПТА"))

    def test_первый_ask_ставит_в_очередь_и_поднимает_pending(self):
        with self.assertRaises(lb.LlmPending):
            lb.ask("claude-sonnet-4-6", "SYS", "СОДЕРЖИМОЕ ПРОМПТА", 3000, R)
        n = db.query("SELECT count(*) n FROM feedback_llm_queue WHERE req_key=%s", (self.k,))[0]["n"]
        self.assertEqual(n, 1)

    def test_повтор_не_плодит_строк(self):
        for _ in range(3):
            with self.assertRaises(lb.LlmPending):
                lb.ask("claude-sonnet-4-6", "SYS", "СОДЕРЖИМОЕ ПРОМПТА", 3000, R)
        n = db.query("SELECT count(*) n FROM feedback_llm_queue WHERE req_key=%s", (self.k,))[0]["n"]
        self.assertEqual(n, 1, "тот же промпт обязан уехать в батч ровно один раз")

    def test_готовый_ответ_возвращается_без_очереди(self):
        db.execute("""INSERT INTO feedback_llm_result (req_key, model, raw)
                      VALUES (%s,%s,%s) ON CONFLICT (req_key) DO UPDATE SET raw=EXCLUDED.raw""",
                   (self.k, "claude-sonnet-4-6", '{"reply":"готово"}'))
        got = lb.ask("claude-sonnet-4-6", "SYS", "СОДЕРЖИМОЕ ПРОМПТА", 3000, R)
        self.assertIn("готово", got)
        used = db.query("SELECT used_at FROM feedback_llm_result WHERE req_key=%s", (self.k,))
        self.assertIsNotNone(used[0]["used_at"], "использованный ответ обязан помечаться")


class SyncModeTest(unittest.TestCase):
    def test_внутри_synchronous_батч_выключен(self):
        was = lb.enabled()
        with lb.synchronous():
            self.assertFalse(lb.enabled())
        self.assertEqual(lb.enabled(), was, "режим обязан восстанавливаться на выходе")

    def test_env_выключает_батч(self):
        prev = os.environ.get("FEEDBACK_LLM_BATCH")
        os.environ["FEEDBACK_LLM_BATCH"] = "0"
        try:
            self.assertFalse(lb.enabled())
        finally:
            if prev is None:
                os.environ.pop("FEEDBACK_LLM_BATCH", None)
            else:
                os.environ["FEEDBACK_LLM_BATCH"] = prev


if __name__ == "__main__":
    unittest.main(verbosity=2)

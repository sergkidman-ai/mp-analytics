"""Verify restoration guards without importing clients or calling any API."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]


class RecoverySafety(unittest.TestCase):
    def test_all_legacy_actions_stop_before_loading_a_record(self):
        replies = []
        namespace = {'os': SimpleNamespace(getenv=lambda key, default: default),
                     'answer_cb': lambda *args: replies.append(args)}
        callback = function('feedback_bot/tg_moderation.py', 'handle_callback', namespace)
        for action in ['snd', 'ovr', 'edt', 'lat', 'skp', 'more']:
            callback({'id': 'callback', 'data': action + ':1'})
        self.assertEqual(len(replies), 6)
        self.assertTrue(all('Старые кнопки отключены' in r[1] for r in replies))

    def test_same_numeric_id_does_not_authorize_old_message(self):
        for action in ['snd', 'ovr', 'edt', 'lat', 'skp']:
            with self.subTest(action=action):
                replies = []
                namespace = {'os': SimpleNamespace(getenv=lambda *args: '0'), 'ALLOWED': {'7'},
                             'runtime': SimpleNamespace(callback_prefix=lambda: 'rev:test:'),
                             '_mod': lambda identifier: {'tg_chat_id': 7, 'tg_msg_id': 200},
                             'answer_cb': lambda *args: replies.append(args)}
                callback = function('feedback_bot/tg_moderation.py', 'handle_callback', namespace)
                callback({'id': 'callback', 'from': {'id': 7}, 'data': 'rev:test:' + action + ':1',
                          'message': {'chat': {'id': 7}, 'message_id': 100}})
                self.assertIn('привязка карточки не подтверждена', replies[0][1])

    def test_publication_hold_applies_even_to_manual_override(self):
        namespace = {'os': SimpleNamespace(getenv=lambda key, default: default), '_reload_env': lambda: None}
        send = function('collectors/feedback_send.py', 'post_answer', namespace)
        for override in [None, 'manual']:
            ok, detail = send({}, 'approved answer', override=override)
            self.assertFalse(ok)
            self.assertIn('публикации выключены', detail)

    def test_restored_and_locally_sent_answers_cannot_be_resent(self):
        for state in [{'is_answered': True, 'posted_ok': None}, {'is_answered': False, 'posted_ok': True}]:
            with self.subTest(state=state):
                namespace = {'os': SimpleNamespace(getenv=lambda *args: '0'), '_reload_env': lambda: None,
                             'db': SimpleNamespace(query=lambda *args: [state])}
                send = function('collectors/feedback_send.py', 'post_answer', namespace)
                ok, detail = send({'platform': 'wb', 'account': 'wb_acc2', 'kind': 'review', 'ext_id': 'case'},
                                  'approved answer', override='manual')
                self.assertFalse(ok)
                self.assertIn('повторная публикация запрещена', detail)

    def test_hidden_answer_text_does_not_reopen_an_inactive_platform_case(self):
        namespace = {'os':SimpleNamespace(getenv=lambda *args:'0'),'_reload_env':lambda:None,
                     'db':SimpleNamespace(query=lambda *args:[{'is_answered':False,'posted_ok':None,'recovery_active_unanswered':False}])}
        send=function('collectors/feedback_send.py','post_answer',namespace)
        ok,detail=send({'platform':'wb','account':'wb_acc1','kind':'review','ext_id':'case'},'answer')
        self.assertFalse(ok)
        self.assertIn('актуальной очереди',detail)


if __name__ == '__main__':
    unittest.main()

# поток: rev
"""Узкое исключение к правилу A1.1: претензия + чистый хендофф → кнопка ✅ доступна.

Решение Сергея 12.09.2026. A1.1 держит претензии на ручном ответе потому, что движок не знает,
что случилось с КОНКРЕТНЫМ экземпляром. Но черновик, который ничего не утверждает и ничего не
обещает, а просто зовёт в чат по QR, этому риску не подвержен — решение по нему оператор
принимает сам. Тест фиксирует ровно границу исключения: что оно пропускает и чего не пропускает.

`./venv/bin/python -m unittest tests.test_gate_handoff -v` — в БД и в сеть не ходит.
"""
import unittest

from reports import publish_gate as gate

CLAIM = {'platform': 'wb', 'account': 'wb_acc1', 'kind': 'review', 'ext_id': 'T',
         'item_id': 1, 'payload': {}, 'rating': 1, 'draft_route': 'review',
         'body': 'Картридж не печатает, полосы по всему листу. Брак!',
         'draft_confidence': 0.9}

HANDOFF = ('Здравствуйте, Светлана! Нам жаль, что возникла такая ситуация с картриджем 067H. '
           'Напишите, пожалуйста, в чат по QR-коду на упаковке — разберёмся и поможем.')


def row(text, tpl='llm', **over):
    r = dict(CLAIM)
    r['draft_text'] = text
    r['draft_grounding'] = dict({'llm': True, 'grounded': True, 'source': 'карточка',
                                 'template_id': tpl}, **over.pop('grounding', {}))
    r.update(over)
    return r


def allow(r, text=None):
    ok, why, _ = gate.verdict_full(r, text if text is not None else r['draft_text'])
    return ok, why


class HandoffExceptionTest(unittest.TestCase):
    def test_чистый_хендофф_модели_пропускается(self):
        ok, why = allow(row(HANDOFF))
        self.assertTrue(ok, why)

    def test_шаблонный_хендофф_пропускается(self):
        ok, why = allow(row('Здравствуйте! Напишите нам в чат по QR-коду на упаковке — '
                            'обязательно разберёмся и поможем.', tpl='neg_general_wb'))
        self.assertTrue(ok, why)

    def test_вердикт_о_причине_поломки_держим(self):
        """Черновик mod=652 (12.09.2026): «это явно производственный дефект». Движок картридж
        не вскрывал — в публичном ответе это признание вины магазина."""
        ok, why = allow(row('Здравствуйте! Сожалеем — это явно производственный дефект. '
                            'Напишите нам в чат по QR-коду на упаковке.'))
        self.assertFalse(ok)
        self.assertIn('претензия, ручной ответ', why)

    def test_обещание_держим(self):
        ok, why = allow(row('Здравствуйте! Заменим картридж. Напишите нам в чат по QR-коду.'))
        self.assertFalse(ok)

    def test_разбор_причин_вместо_хендоффа_держим(self):
        """Хендофф короткий. Длинный текст — это уже разбор, его публикует человек."""
        ok, why = allow(row('Напишите нам в чат по QR-коду на упаковке. ' + 'Подробности. ' * 60))
        self.assertFalse(ok)
        self.assertIn('претензия, ручной ответ', why)

    def test_ответ_по_существу_без_хендоффа_держим(self):
        ok, why = allow(row('Здравствуйте! Картридж исправен, проверьте настройки принтера.'))
        self.assertFalse(ok)
        self.assertIn('претензия, ручной ответ', why)

    def test_правка_оператора_судится_на_общих_основаниях(self):
        """own_draft=False: текст разошёлся с черновиком — исключение не применяется."""
        ok, why = allow(row(HANDOFF), text=HANDOFF + ' Вернём деньги.')
        self.assertFalse(ok)

    def test_остальные_причины_гейта_исключением_не_снимаются(self):
        ok, why = allow(row(HANDOFF, grounding={'code_guard': True}))
        self.assertFalse(ok)
        self.assertTrue(any('code-guard' in w for w in why), why)


if __name__ == '__main__':
    unittest.main(verbosity=2)

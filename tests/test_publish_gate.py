# поток: rev
"""Машинный запрет публикации черновика покупателю (`reports/publish_gate.py`).

Аудит движка автоответов 25.08.2026 показал: сигналы недоверия движок пишет давно
(`grounded=false`, `code_guard`, `qa_guard`, `no_card`, низкая уверенность), но НИ ОДНА развилка
публикации их не читала — 94 % черновиков с маркером недоверия ушли покупателю. Работа №1 —
этот гейт. Тест фиксирует не только «плохое держим», но и обе дыры, через которые запрет можно
обойти: путь «✏️ Править» и досыл отложенного мимо кнопки ✅.

`./venv/bin/python -m unittest tests.test_publish_gate -v` — в БД и в сеть не ходит.
"""
import unittest
from unittest import mock

from reports import publish_gate as gate

BASE = {'platform': 'wb', 'account': 'wb_acc1', 'kind': 'question', 'ext_id': 'T',
        'item_id': 1, 'payload': {}, 'draft_route': 'review',
        'draft_text': 'Да, этот картридж подойдёт к вашему принтеру.',
        'draft_confidence': 0.9,
        'draft_grounding': {'llm': True, 'grounded': True, 'source': 'карточка'}}


def row(**over):
    r = dict(BASE)
    r['draft_grounding'] = dict(BASE['draft_grounding'], **over.pop('grounding', {}))
    r.update(over)
    return r


class VerdictTest(unittest.TestCase):
    def test_чистый_черновик_проходит(self):
        allow, why = gate.verdict(row())
        self.assertTrue(allow, why)
        self.assertEqual(why, [])

    def test_жёсткие_причины_держат(self):
        cases = {
            'route=human': row(draft_route='human'),
            'маркер-заглушка': row(draft_text='⚠️ Нужен человек: непрофильный товар'),
            'нет карточки': row(grounding={'no_card': True}),
            'code-guard': row(grounding={'code_guard': True}),
            'qa-guard': row(grounding={'qa_guard': ['приписка карточке']}),
            'источник — модель': row(grounding={'source': 'модель'}),
            'серия по подстроке': row(grounding={'source': 'карточка-серия'}),
            'подставлен другой лот': row(grounding={'source': '+каталог-после-веба'}),
        }
        for name, r in cases.items():
            with self.subTest(name):
                allow, why = gate.verdict(r)
                self.assertFalse(allow, f'{name} прошло')
                self.assertTrue(why)

    def test_мягкие_причины_только_для_текста_модели(self):
        """`grounded`/`confidence` — самооценка LLM. У шаблонного ответа их нет, и судить
        по отсутствующему полю нельзя: иначе гейт запрёт 4561 отзыв, к которым претензий нет."""
        self.assertFalse(gate.verdict(row(draft_confidence=0.1, grounding={'grounded': False}))[0])
        tmpl = row(kind='review', draft_confidence=None,
                   grounding={'llm': False, 'source': 'шаблон', 'grounded': None})
        self.assertTrue(gate.verdict(tmpl)[0], gate.verdict(tmpl)[1])

    def test_причины_накапливаются_и_режутся_в_одну_строку(self):
        allow, why = gate.verdict(row(draft_confidence=0.2,
                                      grounding={'grounded': False, 'no_card': True}))
        self.assertFalse(allow)
        self.assertGreaterEqual(len(why), 3)
        self.assertLessEqual(len(gate.reason_line(why, 80)), 80)

    def test_текст_оператора_судится_вместо_черновика(self):
        """verdict(row, text) обязан смотреть на переданный текст: маркер-заглушку оператор
        мог заменить настоящим ответом."""
        r = row(draft_text='⚠️ Нужен человек')
        self.assertFalse(gate.verdict(r)[0])
        self.assertTrue(gate.verdict(r, 'Да, подойдёт, ресурс 3000 страниц.')[0])


class SendPathTest(unittest.TestCase):
    """Серверная проверка в `post_answer` — скрыть кнопку ✅ недостаточно."""

    def setUp(self):
        from collectors import feedback_send as fs
        self.fs = fs

    def test_post_answer_держит_плохой_черновик_до_всех_прочих_проверок(self):
        bad = row(draft_confidence=0.3, grounding={'grounded': False, 'source': 'модель'})
        with mock.patch.object(self.fs, '_live', return_value=True), \
             mock.patch.object(self.fs, 'send_wb_question') as send:
            ok, detail = self.fs.post_answer(bad, bad['draft_text'])
        self.assertFalse(ok)
        self.assertTrue(detail.startswith('hold:'), detail)
        send.assert_not_called()

    def test_override_пропускает_текст_человека(self):
        bad = row(draft_confidence=0.3, grounding={'grounded': False, 'source': 'модель'})
        with mock.patch.object(self.fs, '_live', return_value=False):
            ok, detail = self.fs.post_answer(bad, 'Ответ, написанный оператором.',
                                             override='правка оператора 1')
        self.assertTrue(ok)
        self.assertTrue(detail.startswith('dry-run'), detail)


class ClaimAndPromiseTest(unittest.TestCase):
    """Блок A1 (бриф 07.09.2026): претензия, обещание в черновике, «да» без подтверждения карточкой."""

    def test_претензия_держится_всегда(self):
        r = row(kind='review', rating=1, body='Картридж не работает, принтер выдаёт ошибку',
                draft_text='Здравствуйте! Спасибо за отзыв.',
                grounding={'llm': False, 'source': 'шаблон'})
        allow, why = gate.verdict(r)
        self.assertFalse(allow)
        self.assertIn('претензия, ручной ответ', why)

    def test_класс_из_grounding_важнее_текста(self):
        """Класс пишет конвейер (_store); на строке с ним текст заново не разбираем."""
        r = row(kind='review', rating=5, body='Всё отлично', draft_text='Спасибо за отзыв!',
                grounding={'llm': False, 'request_class': 'претензия'})
        self.assertFalse(gate.verdict(r)[0])

    def test_мёртвая_карточка_исключение(self):
        """Правило Сергея 24.08.2026 сильнее A1.1: хендофф по убитой карточке уходит сам."""
        r = row(kind='review', rating=1, body='не работает, брак',
                draft_text='Здравствуйте! Напишите нам…',
                grounding={'llm': False, 'template_id': 'dead_card'})
        allow, why = gate.verdict(r)
        self.assertTrue(allow, why)

    def test_обещание_в_черновике_держится(self):
        for t in ('Оформим возврат средств.', 'Мы бесплатно заменим картридж.',
                  'Дошлём недостающее за наш счёт.', 'Гарантируем компенсацию.'):
            with self.subTest(t):
                allow, why = gate.verdict(row(draft_text='Здравствуйте! ' + t))
                self.assertFalse(allow, t)
                self.assertTrue(any('обещание' in w for w in why), why)

    def test_текст_оператора_стоп_листом_не_судится(self):
        """A1.2 применяется ТОЛЬКО к машинному черновику: свои слова оператор выбирает сам."""
        r = row(draft_text='Здравствуйте! Картридж подойдёт.')
        allow, why = gate.verdict(r, 'Оформим замену, напишите нам в чат.')
        self.assertTrue(allow, why)

    def test_обычный_ответ_стоп_лист_не_трогает(self):
        self.assertTrue(gate.verdict(row(draft_text='Да, подойдёт, ресурс 3000 страниц.'))[0])

    def test_положительный_ответ_без_подтверждения_карточкой(self):
        r = row(draft_text='Здравствуйте! Да, подойдёт для вашего принтера.',
                grounding={'compat': {'asked': ['c5890'], 'matched': [], 'status': 'unknown'}})
        allow, why = gate.verdict(r)
        self.assertFalse(allow)
        # с блока D (08.09.2026) правило зовётся D1, второй законный источник — справочник
        self.assertTrue(any('без карточки и справочника' in w for w in why), why)
        self.assertFalse(any('уверенность' in w for w in why), 'ловить правилом, а не порогом')

    def test_отказ_и_подтверждённая_модель_проходят(self):
        no = row(draft_text='К сожалению, не подойдёт — нужен другой картридж.',
                 grounding={'compat': {'asked': ['lbp646'], 'matched': [], 'status': 'unknown'}})
        self.assertTrue(gate.verdict(no)[0], gate.verdict(no)[1])
        yes = row(draft_text='Да, подойдёт для LBP6030.',
                  grounding={'compat': {'asked': ['lbp6030'], 'matched': ['lbp6030'], 'status': 'yes'}})
        self.assertTrue(gate.verdict(yes)[0], gate.verdict(yes)[1])

    def test_ревью_08_09_ложные_срабатывания(self):
        """Замечания независимого ревью блока A1 — каждое своим случаем."""
        # «вернёмся с ответом» — не обещание возврата денег
        self.assertTrue(gate.verdict(row(draft_text='Вернёмся с ответом в течение дня.'))[0])
        # зачин «Да» при отказе по совместимости — не утверждение
        no = row(draft_text='Да, вопрос понятен: к сожалению, не подойдёт.',
                 grounding={'compat': {'asked': ['c5890'], 'matched': [], 'status': 'unknown'}})
        self.assertTrue(gate.verdict(no)[0], gate.verdict(no)[1])
        # спросили две модели, карточка подтверждает одну — вторая остаётся недоказанной
        mix = row(draft_text='Да, подойдёт к обеим моделям.',
                  grounding={'compat': {'asked': ['lbp6030', 'lbp646'], 'matched': ['lbp6030'],
                                        'status': 'yes'}})
        allow, why = gate.verdict(mix)
        self.assertFalse(allow)
        self.assertTrue(any('lbp646' in w and 'lbp6030' not in w for w in why), why)
        # черновик, изменённый только регистром и пробелами, остаётся НАШИМ черновиком
        r = row(draft_text='Оформим замену товара.')
        self.assertFalse(gate.verdict(r, 'оформим  замену товара.')[0])
        # шаблон мёртвой карточки, переписанный оператором, снова судится по общим правилам
        d = row(kind='review', rating=1, body='не работает, брак',
                draft_text='Здравствуйте! Напишите нам…',
                grounding={'llm': False, 'template_id': 'dead_card'})
        self.assertFalse(gate.verdict(d, 'Заменим картридж, ничего не платите.')[0])


if __name__ == '__main__':
    unittest.main()

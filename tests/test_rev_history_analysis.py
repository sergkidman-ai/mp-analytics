# поток: rev
"""Historical analysis holds publication and preserves the operator's work."""
from contextlib import nullcontext
from unittest import TestCase,main
from unittest.mock import patch
from types import SimpleNamespace
from reports import recovery_history as history,recovery_review as review

class HistoryAnalysis(TestCase):
    def row(self,**kw):
        return dict(dict(platform='yandex',account='ya_acc1',kind='review',ext_id='7',
                         body='',pros='',cons='',rating=5),**kw)

    def test_plain_rating_uses_template_but_substantive_text_uses_engine(self):
        self.assertTrue(history.template_only(self.row()))
        self.assertTrue(history.template_only(self.row(body='Спасибо!')))
        self.assertFalse(history.template_only(self.row(body='Не подошёл к принтеру, как заменить?')))
        self.assertFalse(history.template_only(self.row(kind='question',body='Подойдёт?')))

    def test_owner_decisions_and_source_answers_are_protected(self):
        for change in [dict(final_text='Мой текст'),dict(decided_at=1),dict(moderation_state='skipped'),
                       dict(is_answered=True),dict(posted_ok=True),dict(editing=True)]:
            with self.subTest(change=change):self.assertTrue(history.protected(self.row(**change)))
        self.assertFalse(history.protected(self.row()))

    def test_save_requires_archive_and_never_enables_auto_publication(self):
        calls=[]
        # Context managers are real objects so the same transaction encloses both updates.
        class Cursor:
            rowcount=1
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def execute(self,sql,args):calls.append((sql,args))
        class Conn:
            def cursor(self):return Cursor()
        with patch.object(history.db,'get_conn',return_value=nullcontext(Conn())):
            result=history.store(self.row(), 'Спасибо за оценку!',{},.9,'positive')
        self.assertEqual(result,'prepared')
        sql,args=calls[0]
        self.assertIn('AND f.skipped_old',sql)
        self.assertNotIn('skipped_old=',sql)
        self.assertIn('m.final_text IS NOT NULL',sql)
        self.assertEqual(args[1],'review')

class PendingDraft(TestCase):
    def row(self,**kw):
        return dict(dict(mod_id=4,id=4,state='carded',final_text=None,decided_at=None,
                         platform='wb',account='wb_acc1',kind='question',ext_id='q',
                         body='Подойдёт?',pros=None,cons=None,tg_chat_id=1,tg_msg_id=22),**kw)

    def test_candidate_edits_existing_card_with_send_disabled(self):
        row=self.row();events=[]
        def kb(*a,**kw):
            self.assertFalse(kw['allow_send']);self.assertFalse(kw['allow_override']);return {'inline_keyboard':[]}
        with patch.object(review.runtime,'card_lock',return_value=nullcontext()), \
             patch.object(review,'load',return_value=dict(row)), \
             patch.object(review.db,'execute',side_effect=lambda *a:events.append('save')), \
             patch.object(review.bot,'api',return_value={'ok':True}) as api, \
             patch.object(review.bot,'_kb',side_effect=kb), \
             patch.object(review.bot,'_card',return_value='draft'):
            self.assertTrue(review.stage_candidate(row,'Готовый ответ',{}))
            self.assertEqual(api.call_args.args[0],'editMessageText')
            self.assertEqual(api.call_args.args[1]['message_id'],22)
        self.assertEqual(events,['save'])

    def test_candidate_never_overwrites_manual_edit(self):
        with patch.object(review.runtime,'card_lock',return_value=nullcontext()), \
             patch.object(review,'load',return_value=self.row(final_text='Ручной ответ')), \
             patch.object(review.db,'execute') as execute:
            self.assertFalse(review.stage_candidate(self.row(),'Машинный ответ',{}))
            execute.assert_not_called()

if __name__=='__main__':main()

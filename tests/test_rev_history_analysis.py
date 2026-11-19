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

    def test_pending_candidate_is_saved_without_exposing_it_to_moderation(self):
        for message in (None, 22):
            row = self.row(tg_msg_id=message)
            with patch.object(review.runtime, 'card_lock', return_value=nullcontext()), \
                 patch.object(review, 'load', return_value=row), \
                 patch.object(review.db, 'execute') as save, \
                 patch.object(review.bot, 'api') as telegram:
                self.assertTrue(review.stage_candidate(row, 'Кандидат', {}))
                self.assertEqual(save.call_args.args[1][3].adapted['verify'], 'pending')
                telegram.assert_not_called()

    def test_candidate_never_overwrites_manual_edit(self):
        with patch.object(review.runtime,'card_lock',return_value=nullcontext()), \
             patch.object(review,'load',return_value=self.row(final_text='Ручной ответ')), \
             patch.object(review.db,'execute') as execute:
            self.assertFalse(review.stage_candidate(self.row(),'Машинный ответ',{}))
            execute.assert_not_called()

if __name__=='__main__':main()

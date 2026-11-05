# поток: rev
from unittest import TestCase,main
from unittest.mock import Mock,patch
from reports import archive_questions,archive_question_policy,llm_batch,feedback_today,publish_gate
from collectors import feedback_send

class ArchiveNoAnthropic(TestCase):
    def row(self,**kw):
        r=dict(platform='yandex',account='ya_acc1',kind='question',ext_id='old',item_id='3187',
               skipped_old=True,body='Оригинал?',pros=None,cons=None,product_name='Чернила Epson',rating=None)
        return dict(r,**kw)

    def facts(self,**kw):
        return Mock(for_yandex=Mock(return_value=dict(name='Чернила Epson',kind='совместимый',**kw)))

    def test_ask_blocks_archive_before_cache_or_paid_queue(self):
        with patch.object(llm_batch.db,'query') as query,patch.object(llm_batch.db,'execute') as execute:
            with self.assertRaisesRegex(RuntimeError,'Anthropic is disabled'):
                llm_batch.ask('claude-sonnet-4-6','system','prompt',100,self.row())
            query.assert_not_called();execute.assert_not_called()

    def test_normal_engine_routes_archive_to_source_rules_without_model(self):
        with patch.object(feedback_today,'_llm') as model,patch.object(feedback_today,'_verified') as verify:
            result=feedback_today._answer(Mock(),self.row(),self.facts(),Mock())
            self.assertIn('не оригинальные Epson',result[1])
            self.assertTrue(result[4]['deterministic_verified'])
            model.assert_not_called();verify.assert_not_called()

    def test_controller_is_also_disabled_for_archive(self):
        with patch.object(llm_batch,'ask') as ask:
            _,ground=feedback_today._verified(Mock(),self.row(),'answer','facts',{})
            self.assertEqual(ground['verify'],'not_run_for_archive')
            ask.assert_not_called()

    def test_native_cmyk_is_answered_without_ai(self):
        result=archive_questions.answer(self.row(body='Какие там цвета?'),self.facts(color='Набор CMYK'))
        self.assertIn('пурпурный',result[1]);self.assertTrue(result[4]['deterministic_verified'])

    def test_package_uses_exact_public_contents(self):
        with patch.object(archive_questions,'package_contents',return_value=(['Фотобарабан KX-FAD412A DU'],'public URL')):
            result=archive_questions.answer(self.row(body='Тонер входит в комплект?'),self.facts())
        self.assertEqual(result[1],'Здравствуйте! Нет, в комплекте только фотобарабан.')
        self.assertTrue(result[4]['deterministic_verified'])

    def test_missing_manufacturer_is_not_invented(self):
        result=archive_questions.answer(self.row(body='Оригинал? Кто производитель?'),self.facts())
        self.assertEqual(result[2],'human')
        self.assertIn('Производителя нужно уточнить',result[1])
        self.assertIn('Производитель',result[4]['fact_block'])

    def test_unknown_deleted_item_is_not_called_out_of_domain(self):
        with patch('reports.rev_archive_fact_recovery.native_archive_answer',return_value=None):
            result=archive_questions.answer(self.row(item_id='3490del'),Mock(for_yandex=Mock(return_value=None)))
        self.assertEqual(result[1],'')
        self.assertIn('3490del',result[4]['fact_block'])
        self.assertNotIn('Вне профиля',result[4]['fact_block'])

    def test_source_verification_can_enable_manual_review_without_anthropic(self):
        result=archive_questions.answer(self.row(),self.facts())
        row=dict(self.row(),draft_text=result[1],draft_route='review',draft_grounding=result[4])
        allowed,reasons=publish_gate.verdict(row)
        self.assertTrue(allowed,reasons)

    def test_live_questions_are_not_archive(self):
        with patch.object(archive_question_policy.db,'query',return_value=[]):
            self.assertFalse(archive_question_policy.is_archive_question(self.row(skipped_old=False)))
        self.assertFalse(archive_question_policy.is_archive_question(self.row(platform='wb')))

    def test_exact_scoped_primary_can_confirm_missing_model_without_anthropic(self):
        primary={'scope':{'proof_type':'shared_exact_oem_supply','item_id':'3263',
                         'printer':'M7102DN','listed_reference_model':'M7100DN',
                         'required_native_name_fragment':'DL-420 XL DU','oem_supply':'DL-420P'}}
        native=Mock(for_yandex=Mock(return_value={'name':'Фотобарабан DL-420 XL DU',
                                                 'models':['M7100DN']}))
        with patch.object(archive_questions,'cached_primary',return_value=primary):
            result=archive_questions.answer(self.row(item_id='3263',body='Подойдёт для M7102DN?'),native)
        self.assertTrue(result[4]['deterministic_verified'])
        self.assertEqual(result[1],'Здравствуйте! Да, подойдёт для M7102DN.')

    def test_primary_is_not_transferred_to_a_different_item(self):
        primary={'scope':{'proof_type':'shared_exact_oem_supply','item_id':'3263',
                         'printer':'M7102DN','listed_reference_model':'M7100DN',
                         'required_native_name_fragment':'DL-420 XL DU','oem_supply':'DL-420P'}}
        native=Mock(for_yandex=Mock(return_value={'name':'Фотобарабан DL-420 XL DU','models':['M7100DN']}))
        with patch.object(archive_questions,'cached_primary',return_value=primary), \
             patch.object(archive_questions.answer_cache,'try_hit',return_value=None):
            result=archive_questions.answer(self.row(item_id='other',body='Подойдёт для M7102DN?'),native)
        self.assertFalse(result[4]['deterministic_verified'])

    def test_family_cache_is_not_treated_as_exact_lot_evidence(self):
        native=Mock(for_yandex=Mock(return_value={'name':'Фотобарабан','models':[]}))
        cached=('Да, подойдёт',{'cache':{'family':'OTHER','stale':False}})
        with patch.object(archive_questions,'cached_primary',return_value=None), \
             patch.object(archive_questions.answer_cache,'try_hit',return_value=cached):
            result=archive_questions.answer(self.row(body='Подойдёт для M7102DN?'),native)
        self.assertFalse(result[4]['deterministic_verified'])

class ActualArchiveCap(TestCase):
    def test_fixed_archive_replies_count_even_when_old_draft_route_is_review(self):
        with patch.object(feedback_send.db,'query',side_effect=[[{'name':'rev_archive_publication'}],[{'n':556}]]) as query:
            self.assertEqual(feedback_send.backlog_sent_today('yandex','ya_acc1'),556)
            sql=query.call_args_list[-1].args[0]
            self.assertIn('rev_archive_publication',sql)
            self.assertIn("draft_route='auto' OR EXISTS",sql)
            self.assertIn("Europe/Moscow",sql)

    def test_old_database_without_archive_table_keeps_ordinary_counter(self):
        with patch.object(feedback_send.db,'query',side_effect=[[{'name':None}],[{'n':3}]]) as query:
            self.assertEqual(feedback_send.backlog_sent_today('wb','wb_acc1'),3)
            self.assertNotIn('rev_archive_publication',query.call_args_list[-1].args[0])

if __name__=='__main__':main()

# поток: rev
import contextlib
import unittest
from unittest.mock import patch

from reports import card_facts as cf, card_fact_validation as validation
from reports import feedback_llm, feedback_today, recovery_review, publish_gate


class NativeFacts(unittest.TestCase):
    def test_market_reads_exact_own_offer_even_if_wb_twin_exists(self):
        payload = {'offer':{'offerId':'6068','name':'Картридж C230',
                           'description':'Чип не требуется.', 'manufacturerCountries':['Китай']}}
        with patch.object(cf.db,'query',return_value=[{'payload':payload}]) as query:
            facts = cf.CardFacts().for_yandex('6068','ya_acc1')
        self.assertEqual(facts['platform'],'yandex')
        self.assertEqual(facts['chip'],'not_required')
        self.assertEqual(facts['country'],'Китай')
        self.assertIn('raw_yandex_offer',query.call_args.args[0])
        self.assertEqual(query.call_args.args[1],('ya_acc1','6068'))

    def test_missing_market_card_does_not_read_wb_prefix(self):
        with patch.object(cf.db,'query',return_value=[]) as query:
            self.assertIsNone(cf.CardFacts().for_yandex('unknown'))
            self.assertEqual(query.call_count,1)

    def test_ozon_exact_sku_keeps_its_account_card(self):
        payload={'name':'Exact','attributes':[{'id':4191,'values':[{'value':'Чип не требуется'}]}]}
        with patch.object(cf.db,'query',return_value=[{'payload':payload}]) as query:
            facts=cf.CardFacts().for_ozon('123')
        self.assertEqual(facts['chip'],'not_required')
        self.assertIn('WHERE sku=',query.call_args.args[0])

    def test_ozon_missing_exact_card_does_not_use_sibling(self):
        facts=cf.CardFacts()
        with patch.object(cf.db,'query',side_effect=[[],[{'account':'oz_acc2','offer_id':'5280x'}],[]]), \
             patch.object(facts,'_oz_sibling') as sibling:
            self.assertIsNone(facts.for_ozon('123'))
            sibling.assert_not_called()

    def test_ready_for_use_is_not_a_chip_fact(self):
        self.assertIsNone(cf._classify_chip('Полностью готов к использованию'))
        self.assertEqual(cf._classify_chip('Чип не требуется'),'not_required')
        self.assertNotIn('уже установлен',feedback_llm._CHIP_RU['not_required'])
        self.assertNotIn('переставляется',feedback_llm._CHIP_RU['none'])

    def test_general_kit_survives_and_description_is_not_cut_at_600(self):
        facts=cf.facts_ozon({'name':'Комплект картриджей Epson', 'attributes':[
            {'id':4191,'values':[{'value':'А'*700+' В комплекте шесть картриджей.'}]},
            {'id':9602,'values':[{'value':'Красный'}]}]})
        self.assertTrue(facts['set_info'])
        self.assertIn('шесть',facts['annot'])
        self.assertTrue(validation.problems(facts,'В комплекте только красный картридж.'))

    def test_chip_does_not_imply_level_or_installation(self):
        facts={'chip':'not_required','annot':'Чип не требуется.'}
        self.assertTrue(validation.problems(facts,'Чип уже установлен.'))
        self.assertTrue(validation.problems(facts,'Картридж показывает уровень тонера.'))
        self.assertTrue(validation.problems(facts,'Используется без дополнительных настроек.'))
        self.assertEqual(validation.problems(facts,'Для этого картриджа чип не требуется.'),[])
        self.assertEqual(validation.problems({'annot':'Чип уже установлен.'},'Чип уже установлен.'),[])

    def test_old_pass_cannot_enable_auto(self):
        row={'kind':'question','body':'Есть чип?'}
        ground={'verify':'PASS','source':'карточка','request_class':'характеристики'}
        self.assertEqual(feedback_today._auto_card_q(row,'Чип не требуется.','review',ground)[0],'review')

    def test_fact_change_blocks_old_pass_at_send(self):
        row={'kind':'question','draft_text':'Чип не требуется.','draft_route':'review',
             'body':'Есть чип?','draft_grounding':{'verify':'PASS','facts_signature':validation.signature({'chip':'not_required'})}}
        with patch.object(feedback_today,'_card_facts',return_value={'chip':'installed'}):
            allow,reasons,_=publish_gate.verdict_full(row)
        self.assertFalse(allow)
        self.assertTrue(any('карточка изменилась' in reason for reason in reasons))

    def test_requeue_preserves_operator_decision(self):
        row={'state':'carded','final_text':'Ручной ответ','decided_at':None}
        with patch.object(recovery_review.db,'query',return_value=[{'id':1}]), \
             patch.object(recovery_review,'load',return_value=row), \
             patch.object(recovery_review.runtime,'card_lock',return_value=contextlib.nullcontext()), \
             patch.object(recovery_review.db,'get_conn') as conn:
            self.assertEqual(recovery_review.requeue_changed_facts(),0)
            conn.assert_not_called()


if __name__=='__main__':
    unittest.main()

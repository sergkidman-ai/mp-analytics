# поток: rev
import unittest
from reports.compatibility_evidence import absence_denial
from reports.deepseek_search import cache_key,legacy_cache_key
from reports import publish_gate

class CompatibilityEvidence(unittest.TestCase):
    def test_real_unfounded_denial_is_blocked_even_if_controller_passed(self):
        text=('Здравствуйте! Картриджи TN-217 не предназначены для Brother DCP-L3560CDW — '
              'эта модель в списке совместимости не значится.')
        self.assertTrue(absence_denial(text))
        row=dict(kind='question',draft_text=text,draft_route='review',body='Подходит?',
                 draft_grounding={'verify':'PASS','grounded':True})
        allowed,reasons=publish_gate.verdict(row)
        self.assertFalse(allowed)
        self.assertTrue(any('отсутствие модели' in s for s in reasons))
        self.assertFalse(publish_gate.can_override(reasons))

    def test_unknown_is_not_the_same_as_denial(self):
        self.assertFalse(absence_denial('Совместимость пока не подтверждена. Уточните маркировку картриджа.'))
        self.assertFalse(absence_denial('Нет, для этой версии нужны TN248XL. Источник — руководство Brother.'))
        self.assertFalse(absence_denial('Да, подойдут, совместимость подтверждена изготовителем.'))

    def test_no_model_in_list_alone_is_not_a_denial(self):
        self.assertFalse(absence_denial('Этой модели в списке нет. Сейчас проверяем совместимость.'))

    def test_both_model_and_consumable_survive_long_question_truncation(self):
        q='здравствуйте, у меня Brother DCP-L3560CDW. '+('В описании написано, что подходят. '*10)
        product='Картриджи TN-217 для принтеров Brother DCP-L3550CDW, HL-L3230CDW'
        old,_=legacy_cache_key(q,product);new,_=cache_key(q,product)
        self.assertNotIn('TN-217',old)
        self.assertIn('TN-217',new);self.assertIn('DCP-L3560CDW',new)
        self.assertLessEqual(len(new),128)
        self.assertEqual(cache_key(q,product),cache_key(q,product))

if __name__=='__main__':unittest.main()

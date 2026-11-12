# поток: rev
import copy
import json
import unittest
from unittest.mock import patch
from reports import question_evidence as e, evidence_answers as answers, rev_image_facts as image
from reports import rev_fact_sources as src, llm_batch, publish_gate


class EvidenceTests(unittest.TestCase):
    def test_unselected_supplier_is_not_proof(self):
        p=self.packet('Какой производитель?')
        p['sources'].append(e.source('ms-0','internal_technical','Производитель не выбран'))
        got=e.apply_checks(p,[{'id':'manufacturer','status':'confirmed',
            'quote':'Производитель не выбран','source_ids':['ms-0']}])
        self.assertEqual(got['conditions'][0]['status'],'missing')

    def test_memory_usage_statistics_do_not_invalidate_answers(self):
        with patch.object(e.db,'query',return_value=[{'stamp':'unchanged'}]) as query:
            e.memory_revision(self.row(),'5280')
        self.assertTrue(all("-'hit_count'-'last_hit_at'" in call.args[0] for call in query.call_args_list))
        self.assertIn('WHERE article=%s AND request_class=%s AND question_key=%s',query.call_args_list[0].args[0])
        self.assertEqual(query.call_args_list[0].args[1][0],'5280')

    def test_single_item_set_info_does_not_prove_six_item_variant(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        @contextmanager
        def primary():
            cur=Mock();cur.fetchall.return_value=[{'external_code':'5280','raw':{},'last_seen':None}]
            yield cur
        with patch.object(src,'offer_identity',return_value='5280ABC'),patch.object(src,'primary',primary),patch.object(src,'cached',return_value={
                'payload':{'title':'378XL-478XL','colors_count':6},'source_at':None}):
            result=src.resolve(self.row(),{'name':'Картридж 378XL-478XL красный','set_info':'Картридж 1 шт.'})
        self.assertEqual(result['status'],'unresolved')

    def test_invalid_controller_json_is_never_pass(self):
        from reports.llm_routing import verify
        from types import SimpleNamespace as NS
        for raw in ('not JSON','{broken}', '{"verdict":"UNKNOWN"}'):
            verdict,_,_=verify(lambda **kw:NS(content=[NS(type='text',text=raw)]),
                               'test','question','Вопрос','Ответ','Факты')
            self.assertEqual(verdict,'FIX')

    def test_fresh_tk_snapshot_survives_primary_database_outage(self):
        record={'payload':{'title':'378XL-478XL','colors_count':6},'source_at':None}
        with patch.object(src,'offer_identity',return_value='5280ABC'),patch.object(src,'primary') as primary,patch.object(
                src,'cached',side_effect=lambda kind,key,age:record if key=='5280' else None):
            result=src.resolve(self.row(),{'name':'Комплект 378XL-478XL'})
        primary.assert_not_called()
        self.assertEqual(result['status'],'matched')

    def row(self,question='Все 6 цвета как на фото?'):
        return dict(platform='ozon',account='oz_acc2',kind='question',ext_id='test',item_id='123',
                    body=question,product_name='Комплект 378XL-478XL',pros=None,cons=None,rating=None)

    def packet(self,question=None,**kw):
        row=self.row(question) if question else self.row()
        data=dict(facts={'name':'Комплект 378XL-478XL','color':'Красный','set_info':'Комплект'},
                  identity={'status':'matched','external_code':'5280','fresh':True,
                            'technical':{'title':'378XL-478XL','colors_count':6}},
                  ms=[],choice={},image_info={'items':[]},notes='',approved=[])
        data.update(kw)
        return e.build(row,**data)

    def test_question_has_two_independent_requirements(self):
        self.assertEqual([x['id'] for x in e.conditions('Подойдут для 4303fdw, прошивка 6.28.3.30?')],['compatibility','firmware'])

    def test_spaced_suffix_is_exact_and_firmware_is_not_a_model(self):
        asked,matched=e.matching_models('Подойдут для HP MFP 4303 fdw, прошивка 6.28.3.30?', ['HP Color LaserJet Pro MFP 4303fdw'])
        self.assertEqual(asked,matched);self.assertEqual(len(asked),1)

    def test_other_suffix_is_not_accepted(self):
        asked,matched=e.matching_models('Подойдут для HP 4303fdw?', ['HP Color LaserJet Pro 4303dw'])
        self.assertTrue(asked);self.assertEqual(matched,[])

    def test_printer_match_does_not_confirm_firmware(self):
        p=self.packet('Подойдут для HP 4303fdw, прошивка 6.28.3.30?',facts={'name':'Комплект 230A','models':['HP MFP 4303fdw'],'chip':'installed'})
        c={x['id']:x['status'] for x in p['conditions']}
        self.assertEqual(c,{'compatibility':'confirmed','firmware':'missing'})
        self.assertFalse(answers.complete(p))

    def test_model_cannot_upgrade_firmware_from_generic_quote(self):
        p=self.packet('Прошивка 6.28.3.30 подходит?')
        p['sources'].append(e.source('web','manufacturer_document','Works with HP 4303fdw'))
        got=e.apply_checks(p,[{'id':'firmware','status':'confirmed','quote':'Works with HP 4303fdw','source_ids':['web']}])
        self.assertEqual(next(x for x in got['conditions'] if x['id']=='firmware')['status'],'missing')

    def test_six_colours_override_single_colour_field_for_kit(self):
        colors=['черный','голубой','пурпурный','желтый','красный','серый']
        p=self.packet(image_info={'items':[{'sha256':'image-hash','url':'https://cdn1.ozone.ru/a.jpg',
           'text':'Комплектация набора: '+', '.join(colors),'facts':{'composition_colors':colors}}]})
        self.assertTrue(answers.complete(p));self.assertIn('6 цветов',answers.deterministic(p))
        self.assertIn('серый',answers.deterministic(p))

    def test_conflicting_images_hold_composition(self):
        items=[{'sha256':str(i),'url':'https://cdn1.ozone.ru/'+str(i), 'text':'Комплектация набора',
                'facts':{'composition_colors':colors}} for i,colors in enumerate([['красный','серый'],['голубой','желтый']])]
        p=self.packet(image_info={'items':items});self.assertFalse(answers.complete(p))

    def test_stale_tk_is_context_not_proof(self):
        p=self.packet(identity={'status':'matched','fresh':False,'technical':{'colors_count':6}})
        self.assertEqual(p['conditions'][0]['status'],'missing')

    def test_supplier_choice_chip_conflict_is_visible(self):
        p=self.packet('Есть чип?',facts={'chip':'installed','name':'Картридж'},
                      choice={'items':[{'facts':{'chip':'none'}}]})
        self.assertEqual(p['conditions'][0]['status'],'conflict')

    def test_no_supplier_choice_does_not_block_confirmed_composition(self):
        p=self.packet(choice={'selected':False,'items':[]})
        self.assertEqual(p['conditions'][0]['status'],'confirmed')

    def test_provenance_quote_must_exist_in_cited_source(self):
        p=self.packet('Кто производитель?')
        got=e.apply_checks(p,[{'id':'manufacturer','status':'confirmed','source_ids':['card'],'quote':'Производитель — несуществующая фабрика'}])
        self.assertEqual(got['conditions'][0]['status'],'missing')

    def test_stale_source_quote_cannot_upgrade_a_gap(self):
        p=self.packet('Кто производитель?');p['sources'].append(e.source('old','technical_catalog','Manufacturer Exact Factory',fresh=False))
        got=e.apply_checks(p,[{'id':'manufacturer','status':'confirmed','source_ids':['old'],'quote':'Manufacturer Exact Factory'}])
        self.assertEqual(got['conditions'][0]['status'],'missing')

    def test_unrelated_colour_question_does_not_get_yes_template(self):
        p=self.packet('Есть другие цвета в комплекте?');self.assertIsNone(answers.deterministic(p))

    def test_private_name_projection_drops_financial_and_supplier_details(self):
        d=src.technical_name('Поставщик SECRETNAME, закупочная цена 123 руб. Картридж с чипом, 13 мл')
        self.assertEqual(d['chip'],'installed');self.assertEqual(d['volume_ml'],'13')
        self.assertNotIn('SECRETNAME',json.dumps(d));self.assertNotIn('123',json.dumps(d))

    def test_internal_warning_is_not_sent_as_product_title(self):
        self.assertEqual(src.technical_name('*ВНИМАНИЕ* не подходит для C822'),{'technical_warning':True})

    def test_tk_projection_never_contains_purchase_data(self):
        got=src.public_tc({'title':'230A','buy_price':1,'supplier':'hidden','firmware_limit':0})
        self.assertEqual(got,{'title':'230A','firmware_limit':None})

    def test_media_rejects_private_or_tokenized_urls(self):
        for url in ['http://cdn1.ozone.ru/a','https://127.0.0.1/a','https://ozone.ru.evil.test/a','https://cdn1.ozone.ru/a?token=secret']:
            self.assertFalse(image.safe_url(url))
        self.assertTrue(image.safe_url('https://cdn1.ozone.ru/a.jpg'))

    def test_ocr_requires_composition_label(self):
        self.assertEqual(image.text_facts('Красный, серый, черный — яркая печать'),{})
        self.assertEqual(len(image.text_facts('КОМПЛЕКТАЦИЯ НАБОРА красный серый черный голубой пурпурный желтый')['composition_colors']),6)

    def test_image_prompt_roundtrip_and_cache_identity(self):
        content=[{'type':'text','text':'read image'},{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':'AAAA'}}]
        wrapped='REV_MULTIMODAL_V1:'+json.dumps(content)
        self.assertEqual(llm_batch.message_content(wrapped),content)
        self.assertNotEqual(llm_batch._key('model','image',self.row(),content),llm_batch._key('model','image',self.row(),content[:1]))

    def test_publication_does_not_treat_incomplete_evidence_as_card_proof(self):
        self.assertFalse(publish_gate._card_sourced({'source':'проверенные источники','evidence_conditions':[{'status':'missing'}]}))
        self.assertTrue(publish_gate._card_sourced({'source':'проверенные источники','evidence_conditions':[{'status':'confirmed'}]}))

    def test_unknown_external_host_is_not_a_primary_source(self):
        self.assertTrue(e.primary_url('https://support.hp.com/a'))
        self.assertFalse(e.primary_url('https://hp.com.evil.test/a'))


if __name__=='__main__':unittest.main()

# поток: rev
import unittest
from unittest.mock import patch
from reports import rev_web_evidence as web, question_evidence as e
from reports.rev_fact_sources import digest


class WebEvidenceTests(unittest.TestCase):
    def row(self):
        return dict(platform='wb',account='wb_acc1',kind='question',ext_id='test',
                    body='Для Epson LQ-690II подходит?',draft_grounding={'evidence_version':e.VERSION})

    def test_official_epson_download_domain_and_lookalike(self):
        self.assertTrue(e.primary_url('https://neon.epson-europe.com/content/file.pdf'))
        self.assertFalse(e.primary_url('https://epson-europe.com.example.org/file'))
        self.assertFalse(e.primary_url('https://www.epson.eu/file?access_token=secret'))
        self.assertFalse(e.primary_url('https://secret@www.epson.eu/file'))

    def test_model_suffix_is_not_removed(self):
        self.assertTrue(web.exact_model_in('LQ-690II','For Epson LQ-690II / LQ-690IIN'))
        self.assertFalse(web.exact_model_in('LQ-690II','For Epson LQ-690 and LQ-690IIN'))

    def test_wrong_printer_triggers_exact_manufacturer_search(self):
        wrong={'url':'https://neon.epson-europe.com/wrong','text':'LQ-690 '+('x '*100),'title':'Old printer'}
        right={'url':'https://www.epson.eu/p/31995','text':'LQ-690II '+('x '*100),'title':'Epson LQ-690II'}
        with patch('reports.deepseek_search.targeted',side_effect=[{'documents':[wrong]},{'documents':[right]}]) as search:
            result=web.lookup(self.row(),{'name':'Лента Epson C13S015610BA'},['compatibility'])
        self.assertEqual(search.call_count,2)
        self.assertIn('site:epson.eu',search.call_args.args[0])
        self.assertEqual(result['documents'],[right])
        self.assertEqual(result['attempts'][0]['relevant_primary'],0)

    def test_absent_sources_are_not_a_negative_compatibility_result(self):
        with patch('reports.deepseek_search.targeted',return_value={'documents':[]}):
            result=web.lookup(self.row(),{'name':'Epson ribbon'},['compatibility'])
        self.assertEqual(result['status'],'no_primary')
        packet={'web_search':result,'sources':[]}
        with patch('core.db.query',return_value=[{'payload':packet}]):block=web.operator_block(self.row())
        self.assertIn('Это не означает',block)
        self.assertNotIn('<b>не подходит</b>',block)

    def test_accessory_code_is_kept_even_at_end_of_long_page(self):
        text='Epson LQ-690II\n'+('Specification ' *400)+'\nAccessories: C13S015610\n'
        short=web.focused_text(text,'Лента C13S015610BA Epson LQ-690II')
        self.assertIn('C13S015610',short);self.assertLessEqual(len(short),2400)

    def test_pdf_line_wrapping_does_not_discard_a_real_quote(self):
        packet={'conditions':[{'id':'compatibility','status':'missing'}],
                'sources':[{'id':'web-0','text':'Epson LQ-690II uses\n ribbon S015610','kind':'manufacturer_document'}]}
        result=e.apply_checks(packet,[{'id':'compatibility','status':'confirmed','source_ids':['web-0'],
                                      'quote':'Epson LQ-690II uses ribbon S015610','conclusion':'yes'}])
        self.assertEqual(result['conditions'][0]['conclusion'],'yes')

    def test_operator_gets_link_quote_and_verdict_only_after_verification(self):
        packet={'sources':[{'id':'web-0','kind':'manufacturer_document','title':'Epson <test>',
                           'url':'https://www.epson.eu/p/31995','text':'LQ-690II uses S015610'}],
                'conditions':[{'id':'compatibility','status':'confirmed','source_ids':['web-0'],
                               'conclusion':'yes','value':'LQ-690II uses S015610'}],'conflicts':[]}
        row=self.row();row['draft_grounding'].update(verify='PASS',evidence_signature=digest(packet))
        with patch('core.db.query',return_value=[{'payload':packet}]):block=web.operator_block(row)
        self.assertIn('<b>подходит</b>',block);self.assertIn('href=',block);self.assertIn('Цитата:',block)
        self.assertNotIn('<test>',block)
        row['draft_grounding']['verify']='pending'
        with patch('core.db.query',return_value=[{'payload':packet}]):block=web.operator_block(row)
        self.assertNotIn('<b>подходит</b>',block)

    def test_old_non_evidence_card_does_not_require_new_table(self):
        row=self.row();row['draft_grounding']={}
        with patch('core.db.query') as query:self.assertEqual(web.operator_block(row),'')
        query.assert_not_called()

    def test_missing_compatibility_keeps_literal_accessory_excerpt_without_yes_badge(self):
        packet={'sources':[{'id':'card','kind':'native','text':'Лента C13S015610BA'},
                           {'id':'web-0','kind':'manufacturer_document','title':'Epson LQ-690II',
                            'url':'https://www.epson.eu/p/31995',
                            'text':'Epson LQ-690II\nAccessories\nRibbon for LQ-690 (C13S015610)'}],
                'conditions':[{'id':'compatibility','status':'missing'}],'conflicts':[]}
        with patch('core.db.query',return_value=[{'payload':packet}]):block=web.operator_block(self.row())
        self.assertIn('Цитата: Ribbon for LQ-690 (C13S015610)',block)
        self.assertNotIn('<b>подходит</b>',block)
        self.assertIn('ещё не подтверждён',block)

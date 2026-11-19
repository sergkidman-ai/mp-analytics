from reports.feedback_today import _filled_on_arrival

FACTS = dict(name='Картридж Canon 070', set_info='Картридж с чипом', kind='совместимый',
             chip='installed', resource='3000', annot='Для лазерного принтера')
QUESTION = 'Катридж приходит уже заправленный.?'
ANSWER = 'Здравствуйте! Да, картридж приходит заправленный и готов к работе.'


def test_factory_fill_is_single_intent_and_scoped_to_complete_cartridge():
    assert _filled_on_arrival(QUESTION, ANSWER, FACTS)
    for other in ('Можно ли его заправить?', QUESTION + ' А подходит к HP?', 'Он пустой?'):
        assert not _filled_on_arrival(other, ANSWER, FACTS)
    for data in (dict(chip='none'), dict(annot='Для лазерного принтера, без тонера'), dict(resource=None)):
        assert not _filled_on_arrival(QUESTION, ANSWER, dict(FACTS, **data))
    assert not _filled_on_arrival(QUESTION, ANSWER + ' Подходит ко всем принтерам.', FACTS)

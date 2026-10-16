# поток: ev
"""WB news selection: mandatory rubrics plus material seller changes in full text."""
import re

FEATURED = {'важно': 'Важно', 'условия сотрудничества': 'Условия сотрудничества',
            'api': 'API', 'апи': 'API'}


def tags(chat_type):
    if isinstance(chat_type, (list, tuple)):
        values = [x.get('name', '') if isinstance(x, dict) else str(x) for x in chat_type]
    else:
        values = (chat_type or '').split(',')
    return {re.sub(r'\s+', ' ', x).strip().lower() for x in values if x.strip()}


def reason(title, body='', chat_type=None):
    """Explain why a notice belongs in the feed. SC scope is checked by the caller."""
    rubrics = tags(chat_type)
    required = [label for key, label in FEATURED.items() if key in rubrics]
    if required:
        return 'рубрика WB: ' + ', '.join(dict.fromkeys(required))
    t = re.sub(r'\s+', ' ', title or '').strip().lower().replace('ё', 'е')
    b = re.sub(r'\s+', ' ', body or '').lower().replace('ё', 'е')
    if re.search(r'вебинар|онлайн-встреч|подкаст|\bэфир|запись\s+эфира|новые\s+статьи|приглашаем|пригласим', t):
        return None
    # An automatic tool feature is not the same as enrolment or paid continuation.
    enrol = (r'подключили\s+(?:вам|для\s+ваших|некоторым\s+вашим)|'
             r'(?:автоматически|по\s+умолчанию)[^.]{0,140}(?:подключ|добав\w*\s+ваш\w*\s+товар|включ\w*\s+услуг)|'
             r'начнем\s+(?:автоматически\s+)?списывать|переведем[^.]{0,80}на\s+платн|'
             r'(?:услуг\w*|сервис\w*|инструмент\w*|подписк\w*)[^.]{0,80}станет\s+платн|'
             r'продолжим[^.]{0,80}за\s+рубли')
    if re.search(enrol, t + '\n' + b):
        return 'подключение / изменение оплаты услуги WB (в тексте)'
    # Pair an action/obligation with a seller requirement, rather than selecting
    # any mention of a requirement in an article or an existing help page.
    requirement = (r'нов\w*\s+(?:техническ\w*\s+)?(?:требован|правил|ограничен)|'
                   r'(?:измен\w*|обнов\w*|смягч\w*|ужесточ\w*)[^.!?]{0,80}'
                   r'(?:требован|правил|ограничен|маркиров|упаковк)|'
                   r'(?:требован|правил|ограничен)[^.!?]{0,100}(?:измен|обнов|смягч|ужесточ)|'
                   r'(?:станет|станут|будет|будут)\s+обязательн|'
                   r'начнем\s+проверять[^.!?]{0,120}(?:gtin|документ|маркиров|серийн)|'
                   r'(?:не\s+должен|не\s+должна|не\s+должны)\s+превышать|'
                   r'(?:необходимо|обязательно)\s+(?:указыв|заполня|передава|предостав|подтвержд)')
    if re.search(requirement, t + '\n' + b):
        return 'новые требования / правила WB (в тексте)'
    # Price/fee changes also matter when the headline does not contain a tariff.
    money = (r'\b(?:стоимост|тариф|комисси|штраф|ставк|коэффициент|к\s*с\b|плат\w*\s+обработк)')
    change = (r'\b(?:изменим|изменяем|изменили|изменится|изменятся|'
              r'обновим|обновляем|обновили|обновится|обновятся|'
              r'снизим|снижаем|снизили|снизится|снизятся|'
              r'повысим|повышаем|повысили|повысится|повысятся|'
              r'увеличим|увеличили|увеличиваем|увеличится|увеличатся|'
              r'уменьшим|уменьшили|уменьшаем|уменьшится|уменьшатся|'
              r'пересчитали|пересчитаем|вернем|вернули|отменим|отменили|отменяем)\b')
    for part in [t] + re.split(r'(?<=[.!?])\s+|\n+', (body or '').lower().replace('ё', 'е')):
        if re.search(money + r'[^.!?]{0,100}' + change + '|' + change + r'[^.!?]{0,100}' + money
                     + r'|нов\w*\s+' + money, part):
            return 'изменение тарифов / денежных условий WB (в тексте)'
    service = r'(?:услуг|сервис|инструмент|подписк)'
    launch = r'(?:нов\w*|запуска\w*|запуст\w*|доступн\w*|расширя\w*)'
    if ((re.search(launch + r'[^.!?]{0,100}' + service + '|' + service + r'[^.!?]{0,100}' + launch, t)
         or re.search(r'(?:запускаем|запустили|теперь\s+доступн\w*)[^.!?]{0,80}'
                      r'(?:нов\w*\s+)?' + service, b))
            and not re.search(r'виджет|метрик|калькулятор|дизайн|отчет', t)):
        return 'новая услуга / инструмент WB'
    fbs = 'fbs' in rubrics or re.search(r'\bfbs\b|\bфбс\b|маркетплейс', t)
    routine = r'возобнов|снова\s+принима|временно[^.]{0,80}не\s+принима|приостановлен'
    action = (r'добавил|обнов|измен|расширил|убрали|теперь|покажем|начнем|запланировал|'
              r'перенесите|переносите|привозите|отгружайте|пересчита|пересчитали|смягчил|'
              r'обязател|электронн\w*\s+очеред|автовозврат|сниз|повыс|порогов')
    if fbs and not re.search(routine, t) and re.search(action, t):
        return 'изменения в работе FBS'
    return None

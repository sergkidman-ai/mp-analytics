# поток: ev
"""Selected service/auto-enrolment news from Ozon «Продвижение»; no Telegram."""
import hashlib
import re
from dataclasses import dataclass

from ops.ozon_major_news import publish_scoped

# Both channels verified using 30.06 and 15.07 «Сбор первых отзывов».
CHANNELS = {
    'oz_acc1': 'c3107767-3766-432e-b49b-1266ca5b5eb8',
    'oz_acc2': '2657668e-2ffa-45bb-be85-78e1d61f7224',
}
BRAND_TITLE_PATTERN = r'у\s+вас\s+появились\s+бренды\s+с\s+продвижением[.!]?'


@dataclass(frozen=True)
class Selection:
    reason: str
    label: str
    personal: bool
    action: str


def is_channel(platform, account, chat_id):
    return platform == 'ozon' and bool(chat_id) and CHANNELS.get(account) == chat_id


def is_brand_notice(title):
    return re.fullmatch(BRAND_TITLE_PATTERN, (title or '').strip(), flags=re.I) is not None


def is_source(platform, account, chat_id, title=None):
    # Brand enrolment arrives in a separate UNSPECIFIED channel. Select only
    # the requested notification family, not that entire operational channel.
    return (is_channel(platform, account, chat_id)
            or (platform == 'ozon' and account in CHANNELS and is_brand_notice(title)))


def where_sql():
    pairs = ' OR '.join(f"(account='{a}' AND chat_id='{c}')" for a, c in CHANNELS.items())
    brand = f"(account IN ('oz_acc1','oz_acc2') AND btrim(title) ~* '^{BRAND_TITLE_PATTERN}$')"
    return f"(platform='ozon' AND ({pairs} OR {brand}))"


def analyze(title, body):
    """Read the full text. Automation alone (reports, integrations) is not enrolment."""
    t = re.sub(r'\s+', ' ', title or '').lower()
    b = re.sub(r'\s+', ' ', body or '').lower()
    text = t + '\n' + b
    if is_brand_notice(title):
        return Selection('brand_enabled', 'Появились бренды с продвижением', True,
                         'Проверить бренды, дату окончания пробного периода и плату с продаж в «Товары и цены → Бренды»; отключить ненужное продвижение и участие в «Морковске».')
    # An automatic step after a seller's application is voluntary integration,
    # unlike Ozon enrolling goods by default. Keep other sentences of the news.
    auto_text = ' '.join(s for s in re.split(r'(?<=[.!?])\s+|\n+', body or '')
                         if not re.search(r'после\s+(?:ваш\w*\s+)?(?:заявк|соглас|подтвержд)|'
                                          r'если\s+вы[^.]{0,80}(?:оставите\s+заявку|подтвердите|подключите)', s, re.I))
    auto_text = re.sub(r'\s+', ' ', auto_text).lower()
    if (re.search(r'подключили\s+(?:вам|некоторым\s+вашим|для\s+части\s+ваших)', text)
            or re.search(r'(?:создали\s+и\s+запустили\s+кампанию\s+с\s+вашими\s+товарами|'
                         r'начислили\s+вам\s+[\d\s.,]+\s+бонусов[^\n]{0,100}запустили\s+кампанию)', text)):
        return Selection('enabled', 'Подтверждено подключение / запуск кампании', True,
                         'Проверить товары, кампанию и условия оплаты; остановить, если продвижение не нужно.')
    if (re.search(r'вы\s+изменили\s+бонусную\s+кампанию', t)
            and re.search(r'(?:автоматически\s+списывать\s+рубли|продолжим\s+продвижение[^.]{0,60}за\s+рубли)', b)):
        return Selection('paid_transition', 'Предупреждение о переходе на оплату рублями', True,
                         'Проверить кампанию и остаток бонусов; остановить до их окончания, если расход рублей не нужен.')
    if re.search(r'бонусы\s+на\s+продвижение\s+закончились', t):
        return Selection('bonus_ended', 'Бонусы закончились: проверить статус кампании', True,
                         'Проверить статус кампании и источник оплаты; само уведомление не подтверждает списание рублей.')
    if (re.search(r'товар\w*[^.]{0,120}автоматически\s+(?:попадут|добав\w*|подключ\w*)', auto_text)
            or re.search(r'(?:добавим|добавляем)\s+в\s+инструмент[^.]{0,140}товар', auto_text)
            or re.search(r'(?:автоматически|по\s+умолчанию)[^.]{0,80}(?:подключим|включим)[^.]{0,80}'
                         r'(?:услуг|инструмент|продвижени|кампани|сбор\s+отзывов|подписк)', auto_text)
            or re.search(r'подключим\s+(?:вам|вашим\s+товарам)\b', auto_text)
            or re.search(r'каждую\s+неделю[^.]{0,180}(?:создавать|созда[её]м)[^.]{0,80}кампани', auto_text)):
        return Selection('planned', 'Объявлено автоматическое добавление товаров / создание кампаний', False,
                         'Проверить дату запуска, товары и условия отказа до начала платного периода.')
    if (re.search(r'автоматически\s+(?:увеличим|повысим)[^.]{0,80}(?:минимум|ставк)', b)
            or re.search(r'(?:обновим|изменим|повысим|снизим)\s+(?:минимальные\s+)?ставк', text)
            or (re.search(r'(?:сбор\s+первых\s+отзывов|услуг|инструмент)', text)
                and re.search(r'(?:как\s+изменится\s+плата|измен\w*\s+стоимост\w*\s+услуг|нов\w*\s+тариф)', text))):
        return Selection('terms', 'Меняются ставки / стоимость услуги', False,
                         'Проверить применимость новых условий к действующим товарам и кампаниям до указанной даты.')
    if (re.search(r'(?:нов\w*\s+(?:платн\w*\s+)?(?:услуг|инструмент|возможност)|'
                  r'вскоре\s+к\s+ним\s+добавится)', text)
            and re.search(r'(?:спишем|стоимость\s+услуги|плат[ау]\s+за\s+услугу)', b)):
        return Selection('new_service', 'Новая платная возможность', False,
                         'Проверить способ подключения и стоимость; описание возможности само по себе не означает её подключение.')
    return None


def publish(row, selection, dry=False, planned=None):
    account = row['account'] if selection.personal else None
    value = f"{row['d']}\n{row['title']}\n{(row['body'] or '').strip()}"
    key = 'ozon:promotion:' + (account + ':' if account else '') + hashlib.sha256(value.encode()).hexdigest()
    scope = where_sql() + (f" AND account='{account}'" if account else '')
    details = 'Продвижение — ' + selection.label + '\n\n' + (row['body'] or '')
    return publish_scoped(row, key, scope, account, details, expect=selection.action,
                          dry=dry, planned=planned)

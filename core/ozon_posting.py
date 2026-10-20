# поток: fin
"""Normalize posting prices separately from untouched raw JSON.

Wire schemas coexist: legacy scalar + currency_code and Money {amount,currency}
from FBS list v4 / FBO list v3. Scalar financial_data fields are RUB for these
accounts. This module never changes the realization revenue/points/partner split.
"""
from decimal import Decimal, InvalidOperation
import re


class PostingProtocolError(ValueError):
    pass


def money_amount(value, *, legacy_currency=None, expected_currency=None):
    """Validate Money without converting it. Customer currency may differ from RUB."""
    if isinstance(value, dict):
        if 'amount' not in value or 'currency' not in value:
            raise PostingProtocolError('Money requires amount and currency')
        currency, value = value['currency'], value['amount']
    else:
        currency = legacy_currency
    if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
        raise PostingProtocolError('Missing or invalid Ozon price currency')
    if expected_currency is not None and currency != expected_currency:
        raise PostingProtocolError('Missing or unsupported Ozon price currency')
    if value is None or isinstance(value, (bool, list, dict)):
        raise PostingProtocolError('Invalid Ozon price amount')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise PostingProtocolError('Invalid Ozon price amount') from None
    if not amount.is_finite():
        raise PostingProtocolError('Non-finite Ozon price amount')
    return amount


def rub(value, *, legacy_currency=None):
    return money_amount(value, legacy_currency=legacy_currency, expected_currency='RUB')


def product_price(product):
    # Only the known legacy scalar schema defaults to RUB; Money never does.
    return rub(product['price'], legacy_currency=product.get('currency_code', 'RUB'))


def rub_sql(value, legacy_currency="'RUB'", *, foreign_customer=False):
    """SQL equivalent: NULL stays NULL; unsupported/malformed money fails.

    Expressions are trusted source-code SQL, never user input. Numeric casts
    preserve decimal precision; scalar financial_data retains its RUB contract.
    """
    foreign = f"""WHEN jsonb_typeof({value}) = 'object'
        AND {value}->>'currency' ~ '^[A-Z]{{3}}$' AND {value}->>'currency' <> 'RUB'
        AND jsonb_typeof({value}->'amount') IN ('string','number')
        AND {value}->>'amount' ~ '^-?[0-9]+([.][0-9]+)?$' THEN NULL""" if foreign_customer else ''
    return f"""(CASE
      WHEN {value} IS NULL OR {value} = 'null'::jsonb THEN NULL
      WHEN jsonb_typeof({value}) = 'object'
        AND {value}->>'currency' = 'RUB'
        AND jsonb_typeof({value}->'amount') IN ('string','number')
        AND {value}->>'amount' ~ '^-?[0-9]+([.][0-9]+)?$'
        THEN ({value}->>'amount')::numeric
      WHEN jsonb_typeof({value}) IN ('string','number') AND {legacy_currency} = 'RUB'
        AND {value} #>> '{{}}' ~ '^-?[0-9]+([.][0-9]+)?$'
        THEN ({value} #>> '{{}}')::numeric
      {foreign}
      ELSE ('invalid Ozon RUB price: ' || {value}::text)::numeric END)"""


def posting_money_sql(sql):
    """Expand explicit RUB(alias.price) markers in posting-only SQL.

    Existing quantity, payout, aggregation and financial rules stay unchanged.
    """
    def replace(match):
        alias, field = match.groups()
        currency = f"coalesce({alias}->>'currency_code','RUB')" if field == 'price' else "'RUB'"
        # Cross-border customer amounts cannot be compared to RUB seller prices.
        # NULL is explicit unavailability, never zero or a guessed FX conversion.
        return rub_sql(f"({alias}->'{field}')", currency, foreign_customer=field == 'customer_price')
    sql = re.sub(r'RUB\(([A-Za-z_][\w.]*)\.(price|old_price|customer_price|payout|total_discount_value)\)', replace, sql)
    def quantity(match):
        alias, payload = match.groups()
        return f"""coalesce(({alias}->>'quantity')::numeric,
          (SELECT sum((oz_product->>'quantity')::numeric)
           FROM jsonb_array_elements({payload}->'products') oz_product
           WHERE oz_product->>'sku' = {alias}->>'product_id'))"""
    return re.sub(r'FD_QTY\(([A-Za-z_][\w.]*),\s*([A-Za-z_][\w.]*)\)', quantity, sql)

-- 223 (inv): Тонерстор — снять лимит платежа, оставшийся от отсрочки.
-- 07.09.2026 метод сменили на 'prepayment_balance' (аванс 50 000 ₽ у Цифрового, 25 000 ₽
-- у Дисквэра, порог 10 000 ₽). payment_cap читает ТОЛЬКО ветка `deferred` в
-- po_payment_watch.process_deferred — на аванс он не влияет, но в карточке условий
-- виден и путает. Решение Сергея: убрать.
-- Было: 7807355364 → 100000.0, 7811803918 → 50000.0 (вернуть можно этими значениями).
UPDATE supplier_payment_terms SET payment_cap = NULL, updated_at = now()
 WHERE inn = '9717092410' AND payment_cap IS NOT NULL;

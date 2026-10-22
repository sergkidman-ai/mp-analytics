# Цифровой: сторож заказов, 22.10.2026

Scope: oz_acc1, wb_acc1, ya_acc1 FBS alongside Diskver oz_acc2/wb_acc2.
Read-only marketplace preflight: Ozon 96 orders / last 24h; WB 70 / last24h;
Yandex43 / rolling2days, 3 calls. Credentials never included in artifacts.
WB acc1 requires Marketplace scope: dedicated reference WB_TOKEN_RETURNS_ACC1.
MS reads remain solely SHIPMENT_GUARD_MS_TOKEN, yunusov@dsquare; no webhooks.
Company scoping uses existing shipment_guard.config identities.

Ozon: approved September expense basis per cabinet. WB: existing published
ms_shipment_rules per cabinet, no finance-rule publication in this task.
Yandex: verified active September basis used by promo guard, not older shipment
rule: yaexp:7d041e402d4ec4727b0e016a286072490d5f135ac63eed802e1531e09e1092f4.
MS sale total for WB/Yandex; marketplace price retained separately. Marketplace
funded discounts do not reduce the sale base. Shared 200/150 profit policy intact.

Yandex API: POST v1/businesses/{businessId}/orders (READ), programTypes FBS,
fake=false, dates.creationDateFrom/To, limit20/pageToken, exact orderIds refresh.
Reference: https://yandex.com/dev/market/partner-api/doc/ru/reference/orders/getBusinessOrders
Price snapshots sum item payment+subsidy+cashback, divide by quantity for the
normalized unit price. No buyer/delivery/contact payloads retained.
Unknown status is not alertable; cancellation is terminal; old tracked active
orders continue to be refreshed outside the creation-date window.

Runtime activation: settings.digital_enabled. One shared MS work call per tick:
3orders/100requests/240s, spacing>=2.1s, existing shared lock. No per-account MS
budget multiplier. New feed10min, initial MS due first_seen+10min, repeat60min.
Telegram Natalia/prc remains profit<=0 or confirmed missing_ms only, per-account
gate and dedup. Dashboard filter includes all five cabinets and company labels;
no additional table column. Bootstrap does not backdate first_seen or MS due.

# Deployment, 22.10.2026

Immutable release: /opt/mp-analytics/runtime/customer-guard-a8fcfc788c2c.
Code commits69d0892,4f0744d,a8fcfc7; isolated fin/customer-digital-orders branch.
Migration077 applied. Only own domain/rules/API/HTML published to shared checkout
with original-content verification; other work preserved, main not switched.
WB token reference points to existing Marketplace token for the same seller;
no secret values appear in code, Git, reports or output.

108 tests passed, then22 tests after the Market filter correction. No failed
checks remain. Browser widths390/1100/1280/1440/1920: no horizontal overflow,
no JS errors, all five account selectors HTTP200.

Initial active backlog: Ozon436/90days, WB378/30days, Market93/29days. Market's
actual filter accepts PROCESSING/DELIVERY/PICKUP/UNPAID; attempted unsupported
states were corrected, successful complete bootstrap replaced its checkpoint.
No partial pages were committed. All first_seen timestamps reflect actual ingest;
first MS checks eligible after ten minutes, beginning08:09UTC. Queue drains under
the existing shared budget, not by extra MS workers.

Dedicated MS identity verified. Read-only example from each Digital cabinet:
Ozon incomplete -> purchase_estimate; WB assembling -> actual; Market assembling
-> actual. All matched correct organization and returned calculation signal ok.
24 MS GETs total for this preview; no saved fake checks or Telegram sends.

Activation08:04UTC: runtime.settings.digital_enabled=true; existing enabled,
notifications_enabled, WB gate and intervals unchanged. Timer resumed. Legacy
MS webhook receiver/worker remain inactive. Ozon/WB financial publication and
marketplace prices/orders were not modified.

Rollback: stop customer timer and wait for current run, digital_enabled=false;
previous immutable release3954c5808155 and original four web files are retained
under ignored reports/data/customer-digital-deploy-backup. Do not downgrade
worker while Digital rows remain without explicit account filtering; prefer gate
rollback using the new worker. Additive account migration and order data remain.

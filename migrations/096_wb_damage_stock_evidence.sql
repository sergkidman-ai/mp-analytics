-- поток: fin. User 06.11: affected-warehouse quantities are loss evidence only.
-- Keep raw stock, frozen baseline and document/cost evidence; correct availability only.
CREATE TABLE IF NOT EXISTS wb_damage_stock_warehouse (
 warehouse text PRIMARY KEY,
 purpose text NOT NULL DEFAULT 'Последний известный остаток для подтверждения убытков'
);
INSERT INTO wb_damage_stock_warehouse(warehouse) VALUES
 ('Краснодар'),('Коледино'),('Электросталь'),('Тула'),
 ('Самара (Новосемейкино)'),('Сарапул'),('СПБ Шушары'),('Котовск'),
 ('Волгоград'),('Пенза'),('СЦ Симферополь (Молодежненское)'),('Невинномысск')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW wb_stocks_usable AS
SELECT s.*,
 CASE WHEN e.warehouse IS NOT NULL THEN GREATEST(COALESCE(s.quantity,0),0)
      ELSE LEAST(GREATEST(COALESCE(s.quantity,0),0),COALESCE(l.qty,0)) END AS excluded_loss,
 CASE WHEN e.warehouse IS NOT NULL THEN 0::numeric
      ELSE GREATEST(COALESCE(s.quantity,0)-COALESCE(l.qty,0),0) END AS usable_quantity,
 CASE WHEN e.warehouse IS NOT NULL THEN 0::numeric
      ELSE GREATEST(COALESCE(s.quantity_full,0)-COALESCE(l.qty,0),0) END AS usable_quantity_full
FROM wb_stocks s
LEFT JOIN wb_damage_stock_warehouse e ON e.warehouse=s.warehouse
LEFT JOIN LATERAL (
 SELECT sum(i.qty) qty FROM wb_loss_document d JOIN wb_loss_item i ON i.document_id=d.id
 WHERE d.account=s.account AND d.warehouse=s.warehouse AND i.nm_id=s.nm_id
 AND (d.header_date IS NULL OR s.captured_at>=d.header_date)
) l ON true;

UPDATE mp_stock_history h SET excluded_loss=GREATEST(raw_available,0),
 usable_available=0,usable_total=CASE WHEN raw_total IS NULL THEN NULL ELSE 0 END
FROM wb_damage_stock_warehouse e WHERE h.platform='wb' AND h.warehouse=e.warehouse;
UPDATE mp_inventory_baseline_item b SET excluded_loss=GREATEST(raw_available,0),usable_available=0
FROM wb_damage_stock_warehouse e WHERE b.platform='wb' AND b.warehouse=e.warehouse;
UPDATE mp_stock_capture c SET excluded_loss=h.excluded,usable_available=h.usable
FROM (SELECT platform,account,captured_at,sum(excluded_loss) excluded,sum(usable_available) usable
 FROM mp_stock_history WHERE platform='wb' GROUP BY platform,account,captured_at) h
WHERE c.platform=h.platform AND c.account=h.account AND c.captured_at=h.captured_at;
UPDATE mp_inventory_baseline_capture c SET usable_available=b.usable
FROM (SELECT platform,account,sum(usable_available) usable FROM mp_inventory_baseline_item
 WHERE platform='wb' GROUP BY platform,account) b
WHERE c.platform=b.platform AND c.account=b.account;

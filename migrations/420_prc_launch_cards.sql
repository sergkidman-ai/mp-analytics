-- поток: prc. Individual marketplace observations; legacy model history remains intact.
CREATE TABLE IF NOT EXISTS prc_launch_card (
 showcase text NOT NULL, offer text NOT NULL, card_id text NOT NULL,
 external_code text NOT NULL, role text NOT NULL,
 role_basis text NOT NULL, name text, state text NOT NULL, note text,
 multiplier int, present boolean NOT NULL DEFAULT true,
 card_at date, active_at date, seeded boolean NOT NULL DEFAULT true,
 first_observed timestamptz NOT NULL DEFAULT now(), checked_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(showcase,offer,card_id),
 CHECK(role IN ('main','printer','bundle','unknown')),
 CHECK(state IN ('selling','card','none','unknown'))
);
CREATE INDEX IF NOT EXISTS prc_launch_card_model_idx ON prc_launch_card(external_code,showcase);
CREATE TABLE IF NOT EXISTS prc_launch_card_source (
 showcase text PRIMARY KEY, checked_at timestamptz NOT NULL
);
CREATE OR REPLACE VIEW prc_launch_primary AS
 SELECT b.external_code,b.showcase,
 CASE WHEN h.status IS DISTINCT FROM 'ok' OR obs.showcase IS NULL THEN 'unknown'
      WHEN p.active THEN 'selling' WHEN p.main_count>0 THEN 'card'
      WHEN p.unknown_count>0 THEN 'unknown' ELSE 'none' END state,
 p.card_at,p.active_at selling_at,coalesce(p.seeded,true) seeded,p.offer,
 CASE WHEN h.status IS DISTINCT FROM 'ok' THEN 'Источник не обновлён'
      WHEN obs.showcase IS NULL THEN 'Отдельный учёт карточек ещё не получен'
      WHEN p.main_count=0 AND p.unknown_count>0 THEN 'Есть карточки с неуточнённым типом'
      WHEN p.main_count=0 THEN 'Основная карточка не найдена'
      ELSE p.note END note
 FROM prc_launch_mp b
 LEFT JOIN prc_launch_source_state h USING(showcase)
 LEFT JOIN prc_launch_card_source obs USING(showcase)
 LEFT JOIN (
  SELECT external_code,showcase,count(*) n,
   count(*) FILTER(WHERE role='main') main_count,
   count(*) FILTER(WHERE role='unknown') unknown_count,
   bool_or(role='main' AND state='selling') active,
   min(card_at) FILTER(WHERE role='main') card_at,
   min(active_at) FILTER(WHERE role='main') active_at,
   bool_or(seeded) FILTER(WHERE role='main') seeded,
   min(offer) FILTER(WHERE role='main') offer,
   string_agg(DISTINCT note,'; ') FILTER(WHERE role='main') note
   FROM prc_launch_card WHERE present GROUP BY external_code,showcase
 ) p USING(external_code,showcase);

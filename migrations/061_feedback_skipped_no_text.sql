-- поток: rev
-- Ozon физически не принимает ответ на отзыв БЕЗ ТЕКСТА:
--   POST /v1/review/comment/create → 400 {"code":3, "message":"createComment: cannot comment on empty review"}
-- В очереди автоотправки oz_acc1 таких 903 из 910 — почти вся очередь Озона неотвечаема, и каждый цикл
-- 5 слотов канала сгорали на заведомо отбойных вызовах. Флаг снимает их с очереди навсегда и отделяет
-- «нельзя по правилам площадки» от настоящих ошибок отправки (posted_ok=false), чтобы не портить статистику.
ALTER TABLE raw_feedback ADD COLUMN IF NOT EXISTS skipped_no_text boolean NOT NULL DEFAULT false;

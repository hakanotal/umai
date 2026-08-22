-- Is there anything in here at all? One row per table, biggest first.
-- The fastest way to see what a session actually wrote.
SELECT relname AS table, n_live_tup AS approx_rows
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY n_live_tup DESC, relname;

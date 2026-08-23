-- Steps per local day, with the granularity tripwire.
--
-- `samples` is the column to watch. Health Auto Export can send hourly buckets
-- or one daily total, and a daily total is stamped at local midnight — the same
-- timestamp as the 00:00-01:00 hourly bucket. Switching the automation from
-- hourly to daily therefore overwrites that one row with the whole day's figure
-- while the other 23 survive, giving a day worth roughly twice its truth. A run
-- of days reading `samples = 24` that suddenly reads 25 is that switch.
--
-- Grouped by the LOCAL date via timezone(), not date_trunc on the UTC column:
-- a 23:30 Istanbul sample is 20:30 UTC and belongs to the day it was walked.
SELECT
    date(timezone(u.tz, hm.recorded_at))       AS day,
    sum(hm.value)::int                         AS steps,
    count(*)                                   AS samples,
    min(timezone(u.tz, hm.recorded_at))::time  AS first_sample,
    max(timezone(u.tz, hm.recorded_at))::time  AS last_sample
FROM health_metrics hm
JOIN users u ON u.id = hm.user_id
WHERE hm.metric = 'steps'
GROUP BY 1
ORDER BY 1 DESC
LIMIT 30;

-- Today's raw samples, newest first. The "why is that number wrong" query:
-- if two rows share a timestamp, the natural key collapsed them and one
-- device's contribution was silently overwritten.
SELECT
    timezone(u.tz, hm.recorded_at) AS local_time,
    hm.value                       AS steps,
    hm.unit,
    hm.source,
    hm.ingested_at
FROM health_metrics hm
JOIN users u ON u.id = hm.user_id
WHERE hm.metric = 'steps'
  AND hm.recorded_at >= date_trunc('day', now() AT TIME ZONE u.tz) AT TIME ZONE u.tz
ORDER BY hm.recorded_at DESC;

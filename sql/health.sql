-- What Health Auto Export has delivered. Empty until the phone is pointed at
-- the ingest endpoint over Tailscale.
SELECT
    metric,
    count(*)                      AS readings,
    min(recorded_at)              AS first_seen,
    max(recorded_at)              AS last_seen,
    round(min(value)::numeric, 2) AS min,
    round(max(value)::numeric, 2) AS max
FROM health_metrics
GROUP BY metric
ORDER BY metric;

-- Weight from every source, merged the way tools._weight_rows merges it:
-- manual weigh-ins and health sync are one series.
SELECT occurred_at AS at, value AS kg, 'log_entry: ' || source AS via
FROM log_entries
WHERE kind = 'weight' AND superseded_by IS NULL
UNION ALL
SELECT recorded_at, value, 'health_sync: ' || coalesce(source, '?')
FROM health_metrics
WHERE metric = 'weight_kg'
ORDER BY at DESC;

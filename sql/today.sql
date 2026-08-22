-- Today's totals, computed the way core/tools.day_totals computes them, so a
-- disagreement between this and /summary is a real bug rather than two
-- different definitions of "today".
--
-- Day boundaries are the user's local midnight, not UTC midnight.
WITH bounds AS (
    SELECT
        u.id AS user_id,
        u.tz,
        date_trunc('day', now() AT TIME ZONE u.tz) AT TIME ZONE u.tz             AS day_start,
        (date_trunc('day', now() AT TIME ZONE u.tz) + interval '1 day') AT TIME ZONE u.tz AS day_end
    FROM users u
)
SELECT
    b.tz,
    round(coalesce(sum(fi.kcal), 0)::numeric, 0)      AS kcal,
    round(coalesce(sum(fi.protein_g), 0)::numeric, 1) AS protein_g,
    round(coalesce(sum(fi.carbs_g), 0)::numeric, 1)   AS carbs_g,
    round(coalesce(sum(fi.fat_g), 0)::numeric, 1)     AS fat_g,
    count(*) FILTER (WHERE fi.food_id IS NULL)        AS items_awaiting_lookup
FROM bounds b
LEFT JOIN log_entries e
       ON e.user_id = b.user_id
      AND e.occurred_at >= b.day_start
      AND e.occurred_at <  b.day_end
      AND e.superseded_by IS NULL
      AND e.kind IN ('food', 'drink')
LEFT JOIN food_items fi ON fi.entry_id = e.id
GROUP BY b.tz;

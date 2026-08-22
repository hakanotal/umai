-- What the enrichment job is chasing: items with no foods row, so they are
-- currently contributing zero calories to every total.
--
-- Ordered by how often you have eaten the thing, which is the same order the
-- job researches them in.
SELECT
    fi.detected_name,
    fi.detected_state           AS state,
    count(*)                    AS times_logged,
    round(avg(fi.grams)::numeric, 0) AS avg_grams,
    max(e.occurred_at)          AS last_eaten,
    ea.attempts                 AS research_attempts,
    ea.last_error               AS why_not_yet
FROM food_items fi
JOIN log_entries e ON e.id = fi.entry_id
LEFT JOIN enrichment_attempts ea
       ON ea.detected_name = fi.detected_name
      AND ea.state = fi.detected_state
WHERE fi.food_id IS NULL
  AND fi.recipe_id IS NULL
  AND e.superseded_by IS NULL
GROUP BY fi.detected_name, fi.detected_state, ea.attempts, ea.last_error
ORDER BY count(*) DESC, fi.detected_name;

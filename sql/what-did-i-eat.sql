-- Every logged meal, newest first, one row per item.
--
-- superseded_by IS NULL is the load-bearing filter: a gram correction writes a
-- new entry and points the old one at it, so without this every corrected meal
-- appears twice.
SELECT
    e.occurred_at AT TIME ZONE u.tz          AS eaten_local,
    e.source,
    fi.position                              AS pos,
    fi.detected_name                         AS item,
    fi.detected_state                        AS state,
    fi.grams,
    round(fi.kcal::numeric, 0)               AS kcal,
    round(fi.protein_g::numeric, 1)          AS protein_g,
    round(fi.carbs_g::numeric, 1)            AS carbs_g,
    round(fi.fat_g::numeric, 1)              AS fat_g,
    f.canonical_name_en                      AS matched_to,
    f.trust_tier,
    fi.resolution_method                     AS how_matched,
    fi.grams_source                          AS grams_from
FROM food_items fi
JOIN log_entries e ON e.id = fi.entry_id
JOIN users u       ON u.id = e.user_id
LEFT JOIN foods f  ON f.id = fi.food_id
WHERE e.superseded_by IS NULL
ORDER BY e.occurred_at DESC, fi.position;

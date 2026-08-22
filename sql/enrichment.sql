-- What the background researcher has done, and what it refused to do.
--
-- last_error is the interesting column after a week of real use: it names the
-- dishes the largest model cannot describe consistently, which is exactly the
-- list worth hand-transcribing into a tier 1 row.
SELECT
    ea.detected_name,
    ea.state,
    ea.attempts,
    f.canonical_name_en                    AS became,
    round(f.kcal_per_100g::numeric, 0)     AS kcal_per_100g,
    round(f.protein_g_per_100g::numeric, 1) AS p,
    round(f.carbs_g_per_100g::numeric, 1)   AS c,
    round(f.fat_g_per_100g::numeric, 1)     AS f_g,
    -- The gate every candidate has to pass: macros must reconstruct energy.
    round((4 * f.protein_g_per_100g + 4 * f.carbs_g_per_100g
           + 9 * f.fat_g_per_100g)::numeric, 0) AS atwater_check,
    f.aliases,
    ea.cuisines                            AS asked_with_cuisines,
    ea.model,
    ea.last_error,
    ea.last_attempt_at
FROM enrichment_attempts ea
LEFT JOIN foods f ON f.id = ea.food_id
ORDER BY ea.last_attempt_at DESC NULLS LAST;

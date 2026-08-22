-- The food composition table by trust tier.
--
-- Tier 1 is lab data, 2 a label, 3 a recipe you weighed yourself, 4 a model's
-- best recall. Tier 4 rows are provisional and are never silently fact.
SELECT
    trust_tier,
    source,
    count(*) AS rows,
    round(min(kcal_per_100g)::numeric, 0) AS min_kcal,
    round(max(kcal_per_100g)::numeric, 0) AS max_kcal
FROM foods
GROUP BY trust_tier, source
ORDER BY trust_tier, source;

-- Everything the model wrote, newest first. Worth reading occasionally: these
-- are the numbers that were never verified by a human.
SELECT
    canonical_name_en, state, trust_tier,
    round(kcal_per_100g::numeric, 0)      AS kcal,
    round(protein_g_per_100g::numeric, 1) AS p,
    round(carbs_g_per_100g::numeric, 1)   AS c,
    round(fat_g_per_100g::numeric, 1)     AS f,
    aliases, source_ref, created_at
FROM foods
WHERE source = 'model'
ORDER BY created_at DESC;

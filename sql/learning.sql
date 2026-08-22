-- The personal learning layer: what the system has worked out about your food.
--
-- Both tables were read by the resolver and the prompt builder long before
-- anything wrote to them, so an empty result here after real use is a bug, not
-- a state.

-- Foods you have logged, most frequent first. This is resolution tier 2: a
-- near-tie is broken towards the thing you eat weekly.
SELECT fl.display_name, f.canonical_name_en, fl.times_logged, fl.last_logged_at
FROM food_library fl
JOIN foods f ON f.id = fl.food_id
ORDER BY fl.times_logged DESC, fl.last_logged_at DESC;

-- How much of each you actually serve yourself. Injected into the perception
-- prompt as a hint; needs 3 observations before it counts.
SELECT
    f.canonical_name_en,
    round(pp.median_grams::numeric, 0) AS median_g,
    round(pp.p25_grams::numeric, 0)    AS p25_g,
    round(pp.p75_grams::numeric, 0)    AS p75_g,
    pp.n_observations,
    pp.updated_at
FROM portion_priors pp
JOIN foods f ON f.id = pp.food_id
ORDER BY pp.n_observations DESC;

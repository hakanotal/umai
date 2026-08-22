-- Every correction, with the entry it replaced.
--
-- The correction rate on photo estimates is one of the plan's process metrics:
-- it should fall as the food library grows, and if it does not, the library is
-- not working.
SELECT
    c.corrected_at,
    fi.detected_name AS item,
    c.field,
    c.old_value,
    c.new_value,
    c.applied_to_library,
    c.applied_to_prior,
    old_e.id         AS superseded_entry,
    old_e.superseded_by AS replaced_by
FROM corrections c
LEFT JOIN food_items fi ON fi.id = c.food_item_id
LEFT JOIN log_entries old_e ON old_e.id = c.entry_id
ORDER BY c.corrected_at DESC;

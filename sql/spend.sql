-- What the bot costs to run. The plan budgets roughly $1.36/month.
SELECT
    date_trunc('day', called_at)::date AS day,
    purpose                             AS task,
    model,
    count(*)                            AS calls,
    sum(input_tokens)                   AS tokens_in,
    sum(output_tokens)                  AS tokens_out,
    round(sum(cost_usd), 4)             AS usd,
    round(avg(latency_ms))              AS avg_ms
FROM api_usage
GROUP BY 1, 2, 3
ORDER BY 1 DESC, usd DESC;

-- Running total.
SELECT round(sum(cost_usd), 4) AS total_usd, count(*) AS calls FROM api_usage;

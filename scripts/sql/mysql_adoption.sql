-- GPT rollout MySQL adoption metrics.
--
-- Consumed by scripts/local_data_bridge.py via the /query/mysql/adoption
-- endpoint. The bridge substitutes two placeholders before sending the query
-- to PyMySQL:
--
--   __BRAND_IDS__   replaced with a parameterized placeholder list whose
--                   length matches the number of brand ids requested.
--   one PyMySQL    bound placeholder for the "days" window (the only one
--   placeholder    used by this file).
--
-- The actual placeholder characters are intentionally omitted from this
-- comment block because PyMySQL counts every percent-s in the query string
-- when binding parameters, including ones inside SQL comments.
SELECT
    brand.id AS brand_id,
    brand.name AS brand_name,
    COUNT(DISTINCT audiobot_practice_session.video_id) AS total_sessions,
    COUNT(audiobot_practice_session.id) AS total_utterances,
    ROUND(
        COUNT(audiobot_practice_session.id) /
        COUNT(DISTINCT audiobot_practice_session.video_id),
        2
    ) AS avg_utterances_per_session
FROM story
LEFT JOIN account ON account.id = story.account_id
JOIN video ON video.story_id = story.id
JOIN audiobot_practice_session
    ON audiobot_practice_session.video_id = video.id
    AND audiobot_practice_session.node_key = 'gpt_test'
JOIN brand
    ON brand.id = (
        CASE
            WHEN story.brand_id IS NOT NULL
                 AND story.brand_id != 0
            THEN story.brand_id
            ELSE account.brandName
        END
    )
WHERE story.rule_engine = 'gpt_1.0'
AND story.inactive NOT IN (1, 2)
AND video.created_time >= NOW() - INTERVAL %s DAY
AND brand.id IN (__BRAND_IDS__)
GROUP BY
    brand.id,
    brand.name
ORDER BY total_sessions DESC;

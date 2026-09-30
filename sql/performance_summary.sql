-- Descriptive counts only; this is not a profit, ROI, or betting-advice calculation.
SELECT p.stat_type,
       p.recommended_side,
       g.result,
       COUNT(*) AS prop_count
FROM posted_props AS p
JOIN latest_grades AS g USING (alert_id)
GROUP BY p.stat_type, p.recommended_side, g.result
ORDER BY p.stat_type, p.recommended_side, g.result;

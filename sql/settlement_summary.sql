-- Separate inferred settlement from original NBA box-score results.
-- NULL grade_json indicates a migrated event whose raw snapshot awaits reimport.
SELECT COALESCE(json_extract(grade_json, '$.settlement.status'), 'legacy_unassessed') AS assessment_status,
       COALESCE(json_extract(grade_json, '$.settlement.result'), 'unresolved') AS settlement_result,
       COALESCE(json_extract(grade_json, '$.settlement.reason'), 'legacy_unassessed') AS reason,
       COUNT(*) AS alert_count
FROM latest_grades
GROUP BY assessment_status, settlement_result, reason
ORDER BY assessment_status, settlement_result, reason;

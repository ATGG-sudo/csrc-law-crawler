-- DuckDB-compatible reconstruction queries for the bounded report snapshot.
-- The CSV inputs are outputs of the read-only 2026-07-16 private-compliance audit.

SELECT
  priority,
  severity,
  issue_family,
  affected,
  unit,
  confidence,
  evidence,
  recommended_action
FROM read_csv_auto(
  '/home/anjie/projects/csrc-law-crawler/tmp/audits/private_compliance_full_20260716/output/adjudicated_issue_families.csv',
  header = true
)
ORDER BY priority ASC, affected DESC, issue_family ASC;

SELECT
  check,
  result,
  numerator,
  denominator,
  numerator::DOUBLE / NULLIF(denominator, 0) AS rate,
  note
FROM read_csv_auto(
  '/home/anjie/projects/csrc-law-crawler/tmp/audits/private_compliance_full_20260716/output/positive_checks.csv',
  header = true
);

SELECT
  825 AS population,
  146 AS rule_lane,
  1008 AS official_urls,
  597 AS amac_title_matches,
  5 AS critical_families,
  7 AS high_families,
  291 AS valid_local_pdfs;

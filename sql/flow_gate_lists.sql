-- G5: members of the two Brevo exclusion lists for HAND-SENT campaigns (contacts only, nothing is sent).
-- NO_PROMO = manual + all B2B (exclude from the Tuesday akcija); NO_ANY = manual + B2B without shop (exclude from edu).
CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control.flow_gate_list_members` AS
WITH
orgs AS (
  SELECT id AS org_id, SAFE_CAST(JSON_VALUE(raw_json, '$.custom_fields."036687330a0d889920e7166c94392ca6238c0115"') AS INT64) AS v309
  FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_orgs`),
persons AS (
  SELECT org_id, LOWER(TRIM(e)) AS email
  FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_persons`,
       UNNEST(ARRAY_CONCAT([email_primary], SPLIT(IFNULL(email_all, ''), ','))) AS e
  WHERE e IS NOT NULL AND TRIM(e) LIKE '%@%' AND org_id IS NOT NULL),
p11_org AS (
  SELECT DISTINCT org_id FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_deals`
  WHERE pipeline_id = 11 AND status = 'open' AND NOT IFNULL(is_deleted, FALSE) AND org_id IS NOT NULL),
b2b_org AS (
  SELECT org_id FROM orgs WHERE v309 = 716
  UNION DISTINCT
  SELECT SAFE_CAST(SUBSTR(customer_key, 5) AS INT64) FROM `jaunais-za-aizv04022026.legacy_paytraq.b2b_shop_flow_classification_v2`
  WHERE flow = 'B2B' AND STARTS_WITH(customer_key, 'org:')),
paid_email AS (
  SELECT DISTINCT LOWER(TRIM(email)) AS email FROM `jaunais-za-aizv04022026.business_marts.mozello_orders`
  WHERE payment_status = 'paid' AND email IS NOT NULL),
m_email AS (
  SELECT DISTINCT cm.master_key, LOWER(TRIM(x)) AS email
  FROM `jaunais-za-aizv04022026.business_marts.customer_master` cm, UNNEST(cm.deliverable_emails) AS x
  WHERE x IS NOT NULL AND TRIM(x) LIKE '%@%'),
by_master AS (
  SELECT me.email, g.gate FROM m_email me JOIN `jaunais-za-aizv04022026.mkt_control.flow_gate_master` g USING (master_key)
  WHERE g.gate IN ('MANUAL_P11', 'B2B_EDU_ONLY', 'B2B_NO_SHOP', 'B2B_SHOP_UNKNOWN')),
by_org AS (   -- PD people of manual / B2B organisations that have no buyer master (cold B2B, leads)
  SELECT p.email, IF(p.org_id IN (SELECT org_id FROM p11_org), 'MANUAL_P11',
                     IF(p.email IN (SELECT email FROM paid_email), 'B2B_EDU_ONLY', 'B2B_NO_SHOP')) AS gate
  FROM persons p
  WHERE (p.org_id IN (SELECT org_id FROM p11_org) OR p.org_id IN (SELECT org_id FROM b2b_org))
    AND p.email NOT IN (SELECT email FROM m_email)),
allm AS (SELECT * FROM (SELECT * FROM by_master UNION ALL SELECT * FROM by_org)
         WHERE REGEXP_CONTAINS(email, r'^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$'))
SELECT 'NO_PROMO' AS list_name, email, STRING_AGG(DISTINCT gate ORDER BY gate) AS why FROM allm GROUP BY email
UNION ALL
SELECT 'NO_ANY', email, STRING_AGG(DISTINCT gate ORDER BY gate) FROM allm
WHERE gate IN ('MANUAL_P11', 'B2B_NO_SHOP', 'B2B_SHOP_UNKNOWN') GROUP BY email

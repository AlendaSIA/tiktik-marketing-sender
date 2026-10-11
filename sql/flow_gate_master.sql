-- FLOW GATE (MAIN 11.10, Raivis D1-D7): one row per master - MANUAL / B2B / shop purchases. SHADOW: read by nothing live.
-- G1 MANUAL     = a PD org (or person) linked to the master has an OPEN deal in pipeline 11 "B2B pardosana"
-- B2B source    = legacy_paytraq.b2b_shop_flow_classification_v2 flow='B2B' (cid:/org: key) OR PD org field 309 = 716
-- SHOP purchase = >= 1 PAID Mozello order (business_marts.mozello_orders, 2020->) whose e-mail belongs to the master
--                 OR whose company reg./VAT number equals one of the master's Paytraq cards (measured 11.10: RIMAL,
--                 Senlejas buy in the shop under other e-mails)
-- G4            = MANUAL and a paid shop order created AFTER the open p11 deal's add_time -> treated as shop flow
CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control.flow_gate_master` AS
WITH
orgs AS (
  SELECT id AS org_id, name, paytraq_client_id AS pt,
         SAFE_CAST(JSON_VALUE(raw_json, '$.custom_fields."036687330a0d889920e7166c94392ca6238c0115"') AS INT64) AS v309
  FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_orgs`),
persons AS (
  SELECT id AS person_id, org_id, LOWER(TRIM(e)) AS email
  FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_persons`,
       UNNEST(ARRAY_CONCAT([email_primary], SPLIT(IFNULL(email_all, ''), ','))) AS e
  WHERE e IS NOT NULL AND TRIM(e) != ''),
p11 AS (
  SELECT id AS deal_id, org_id, person_id, add_time, title
  FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_deals`
  WHERE pipeline_id = 11 AND status = 'open' AND NOT IFNULL(is_deleted, FALSE)),
m_email AS (
  SELECT DISTINCT master_key, email FROM (
    SELECT cm.master_key, LOWER(TRIM(x)) AS email
    FROM `jaunais-za-aizv04022026.business_marts.customer_master` cm, UNNEST(cm.deliverable_emails) AS x
    UNION ALL
    SELECT master_key, LOWER(TRIM(email_norm)) FROM `jaunais-za-aizv04022026.business_marts.customer_identity`)
  WHERE master_key IS NOT NULL AND email IS NOT NULL AND email != ''),
m_org AS (
  SELECT DISTINCT master_key, org_id, via FROM (
    SELECT i.master_key, o.org_id, 'paytraq_client' AS via
    FROM `jaunais-za-aizv04022026.business_marts.customer_identity` i JOIN orgs o ON o.pt = i.client_id
    UNION ALL
    SELECT me.master_key, p.org_id, 'person_email'
    FROM m_email me JOIN persons p USING (email) WHERE p.org_id IS NOT NULL)),
manual AS (
  SELECT master_key, MIN(add_time) AS manual_since, STRING_AGG(DISTINCT CAST(deal_id AS STRING), ',') AS deals
  FROM (SELECT mo.master_key, d.deal_id, d.add_time FROM m_org mo JOIN p11 d USING (org_id)
        UNION ALL
        SELECT me.master_key, d.deal_id, d.add_time
        FROM p11 d JOIN persons p ON p.person_id = d.person_id JOIN m_email me USING (email))
  GROUP BY 1),
cls AS (
  SELECT customer_key, flow FROM `jaunais-za-aizv04022026.legacy_paytraq.b2b_shop_flow_classification_v2`),
b2b AS (
  SELECT master_key, STRING_AGG(DISTINCT src, '+' ORDER BY src) AS b2b_source FROM (
    SELECT SUBSTR(customer_key, 1) AS master_key, 'class_v2' AS src FROM cls
    WHERE flow = 'B2B' AND STARTS_WITH(customer_key, 'cid:')
    UNION ALL
    SELECT mo.master_key, 'class_v2' FROM cls JOIN m_org mo ON cls.customer_key = CONCAT('org:', CAST(mo.org_id AS STRING))
    WHERE cls.flow = 'B2B'
    UNION ALL
    SELECT mo.master_key, 'pd_309' FROM m_org mo JOIN orgs o USING (org_id) WHERE o.v309 = 716)
  GROUP BY 1),
paid AS (
  SELECT order_id, created_at, LOWER(TRIM(email)) AS email,
         ARRAY(SELECT DISTINCT r FROM UNNEST([REGEXP_REPLACE(IFNULL(company_id, ''), r'[^0-9]', ''),
                                              REGEXP_REPLACE(IFNULL(vat_id, ''), r'[^0-9]', '')]) r
               WHERE LENGTH(r) >= 8) AS regs
  FROM `jaunais-za-aizv04022026.business_marts.mozello_orders` WHERE payment_status = 'paid'),
m_reg AS (   -- the company registration numbers of the master's Paytraq cards (a shop order often carries another e-mail)
  SELECT DISTINCT i.master_key, r AS reg
  FROM `jaunais-za-aizv04022026.business_marts.customer_identity` i
  JOIN `jaunais-za-aizv04022026.paytraq_core.clients` c USING (client_id),
       UNNEST([REGEXP_REPLACE(IFNULL(c.reg_number, ''), r'[^0-9]', ''), REGEXP_REPLACE(IFNULL(c.vat_number, ''), r'[^0-9]', '')]) r
  WHERE LENGTH(r) >= 8),
shop_hit AS (
  SELECT me.master_key, p.order_id, p.created_at, 'email' AS via FROM paid p JOIN m_email me USING (email)
  UNION ALL
  SELECT mr.master_key, p.order_id, p.created_at, 'reg_nr' FROM paid p, UNNEST(p.regs) r JOIN m_reg mr ON mr.reg = r),
shop AS (
  SELECT master_key, COUNT(DISTINCT order_id) AS paid_shop_orders, MAX(created_at) AS last_paid_shop,
         COUNT(DISTINCT IF(via = 'reg_nr', order_id, NULL)) AS paid_by_reg_only_candidates,
         ARRAY_AGG(STRUCT(order_id, created_at) ORDER BY created_at DESC LIMIT 20) AS recent
  FROM shop_hit GROUP BY 1),
masters AS (
  SELECT master_key FROM `jaunais-za-aizv04022026.business_marts.customer_master` WHERE master_key IS NOT NULL),
j AS (
  SELECT m.master_key,
         ma.manual_since, ma.deals AS p11_deals,
         b.b2b_source,
         IFNULL(s.paid_shop_orders, 0) AS paid_shop_orders, s.last_paid_shop, s.paid_by_reg_only_candidates,
         (SELECT AS STRUCT r.order_id, r.created_at FROM UNNEST(s.recent) r
          WHERE ma.manual_since IS NOT NULL AND r.created_at > ma.manual_since ORDER BY r.created_at LIMIT 1) AS shop_after_manual,
         EXISTS(SELECT 1 FROM m_email me WHERE me.master_key = m.master_key)
           OR EXISTS(SELECT 1 FROM m_reg mr WHERE mr.master_key = m.master_key) AS has_email
  FROM masters m LEFT JOIN manual ma USING (master_key) LEFT JOIN b2b b USING (master_key) LEFT JOIN shop s USING (master_key))
SELECT j.*,
  CASE
    WHEN manual_since IS NOT NULL AND shop_after_manual.order_id IS NULL THEN 'MANUAL_P11'
    WHEN manual_since IS NOT NULL THEN 'MANUAL_TO_SHOP'                 -- G4: treated as shop flow from the next plan
    WHEN b2b_source IS NOT NULL AND NOT has_email THEN 'B2B_SHOP_UNKNOWN'
    WHEN b2b_source IS NOT NULL AND paid_shop_orders > 0 THEN 'B2B_EDU_ONLY'
    WHEN b2b_source IS NOT NULL THEN 'B2B_NO_SHOP'
    ELSE 'NONE' END AS gate,
  CURRENT_TIMESTAMP() AS built_at
FROM j


WITH pp AS (WITH idn AS (SELECT DISTINCT email_norm, master_key FROM `jaunais-za-aizv04022026.business_marts.customer_identity`
             WHERE master_key IS NOT NULL AND email_norm IS NOT NULL),
d AS (SELECT id, title, person_id, DATE(add_time, 'Europe/Riga') AS order_on, stage_id, status, won_time,
             stage_change_time
      FROM `jaunais-za-aizv04022026.channel_raw.pipedrive_deals`
      WHERE pipeline_id = 6 AND NOT IFNULL(is_deleted, FALSE) AND status != 'lost'
        AND add_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 180 DAY)),
pw AS (SELECT order_ref, MIN(staged_at) AS staged, ANY_VALUE(LOWER(TRIM(email))) AS email
       FROM `jaunais-za-aizv04022026.business_marts.parcel_watch` WHERE order_ref IS NOT NULL GROUP BY 1),
x AS (SELECT d.id, d.title, d.order_on, d.person_id, COALESCE(p1.email, p2.email) AS parcel_email,
        COALESCE(DATE(p1.staged, 'Europe/Riga'), DATE(p2.staged, 'Europe/Riga'),
                 IF(d.stage_id = 68 AND d.status = 'won', DATE(d.won_time, 'Europe/Riga'), NULL),
                 IF(d.stage_id IN (62, 63, 64, 65, 66), DATE(d.stage_change_time, 'Europe/Riga'), NULL)) AS ship_on,
        CASE WHEN p1.staged IS NOT NULL OR p2.staged IS NOT NULL THEN 'parcel_staged'
             WHEN d.stage_id = 68 AND d.status = 'won' THEN 'picked_up'
             WHEN d.stage_id IN (62, 63, 64, 65, 66) THEN 'stage_change' END AS ship_src
      FROM d LEFT JOIN pw p1 ON p1.order_ref = d.title LEFT JOIN pw p2 ON p2.order_ref = CAST(d.id AS STRING)),
em AS (SELECT x.id, LOWER(TRIM(e)) AS email FROM x
       JOIN `jaunais-za-aizv04022026.channel_raw.pipedrive_persons` p ON p.id = x.person_id,
            UNNEST(SPLIT(IFNULL(p.email_all, ''), ',')) e WHERE TRIM(e) NOT IN ('', 'nan')
       UNION DISTINCT SELECT id, parcel_email FROM x WHERE parcel_email IS NOT NULL),
m AS (SELECT DISTINCT em.id, idn.master_key FROM em JOIN idn ON idn.email_norm = em.email)
SELECT m.master_key,
       ARRAY_AGG(STRUCT(x.title AS order_nr, x.order_on, x.ship_on, x.ship_src)
                 ORDER BY x.order_on DESC, x.id DESC LIMIT 1)[OFFSET(0)] AS o
FROM m JOIN x USING (id) GROUP BY 1),
pick AS (
  SELECT master_key, ROW_NUMBER() OVER (PARTITION BY email_type, IFNULL(hold_reason,'') ORDER BY planned_send_date, FARM_FINGERPRINT(master_key)) rn
  FROM `jaunais-za-aizv04022026.mkt_control.shadow_send_plan`
  WHERE plan_date = CURRENT_DATE() AND run_id = 'tiktik-shadow-plan-76gwz' AND email_type IS NOT NULL
    AND (would_send OR hold_reason IN ('no_offer_valid_until','PP_NOT_SHIPPED','ORDER_AFTER_LAST_PURCHASE'))),
lc AS (SELECT master_key, lifecycle_stage, last_order, first_order, entry_threshold_days, orders,
         ROW_NUMBER() OVER (PARTITION BY master_key ORDER BY last_order DESC, email) rn
       FROM `jaunais-za-aizv04022026.business_marts.customer_lifecycle` WHERE master_key IS NOT NULL)
SELECT p.master_key, CONCAT(SUBSTR(p.email,1,2),'***@',SUBSTR(SPLIT(p.email,'@')[SAFE_OFFSET(1)],1,1),'***.',ARRAY_REVERSE(SPLIT(p.email,'.'))[OFFSET(0)]) AS em,
  p.email_type, p.template_id, p.planned_send_date, p.would_send, p.hold_reason, p.presend_gates, p.would_deliver, p.offer_rung,
  p.offer_valid_until, p.trigger_order_nr, p.xsell_valid_until, p.reason, p.flow,
  s.track, s.track_entered_on, s.step, s.rung, s.rung_set_on, s.rung_month, s.ladder_cleared_on, s.last_email_type, s.last_sent_on,
  s.rung_cap, s.rung_cap_until, s.reorder_worked_at,
  lc.lifecycle_stage, lc.last_order, lc.first_order, lc.entry_threshold_days, lc.orders,
  pp.o.order_nr, pp.o.order_on, pp.o.ship_on, pp.o.ship_src,
  a.in_audience AS akcija_in, a.excluded_reason AS akcija_reason
FROM pick JOIN `jaunais-za-aizv04022026.mkt_control.shadow_send_plan` p ON p.master_key = pick.master_key AND p.plan_date = CURRENT_DATE() AND p.run_id = 'tiktik-shadow-plan-76gwz'
JOIN `jaunais-za-aizv04022026.mkt_control.contact_sequence_state` s ON s.master_key = p.master_key
JOIN lc ON lc.master_key = p.master_key AND lc.rn = 1
LEFT JOIN pp ON pp.master_key = p.master_key
LEFT JOIN `jaunais-za-aizv04022026.mkt_control.shadow_akcija_audience` a ON a.master_key = p.master_key AND a.plan_date = CURRENT_DATE()
WHERE pick.rn <= 5 ORDER BY p.email_type, p.hold_reason, p.planned_send_date

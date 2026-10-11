-- FLOW GATE applied to one send_plan run (SHADOW, D1): the run's rows with the gate's decision. Nothing reads it live.
CREATE OR REPLACE TABLE `jaunais-za-aizv04022026.mkt_control.flow_gate_plan_shadow` AS
SELECT p.run_id, p.planned_at, p.master_key, p.email, p.track, p.email_type, p.template_id, p.decision,
  IFNULL(g.gate, 'NONE') AS gate,
  CASE
    WHEN g.gate = 'MANUAL_P11' THEN 'MANUAL_P11'                                        -- G1: no commercial, no edu
    WHEN g.gate IN ('B2B_NO_SHOP', 'B2B_SHOP_UNKNOWN') THEN 'B2B_NO_SHOP'               -- G3: nothing
    WHEN g.gate = 'B2B_EDU_ONLY' AND p.track != 'educational' THEN 'B2B_EDU_ONLY'       -- G2: educational only
    ELSE NULL END AS gate_drop,
  CURRENT_TIMESTAMP() AS built_at
FROM `jaunais-za-aizv04022026.mkt_control.send_plan` p
LEFT JOIN `jaunais-za-aizv04022026.mkt_control.flow_gate_master` g USING (master_key)
WHERE p.run_id = @run_id

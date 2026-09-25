-- email_template_map rows for the engine letters 229-236 (MAIN command 6C, 2026-09-25).
-- PREPARED, NOT APPLIED. Applying it is MAIN's decision. Owner of these rows: Vestulu sabloni
-- (MAIN 2026-09-25 16:15: "Vestulu sabloni owns: templates, draft/pre-send content checks,
-- email_template_map rows"). Written to the TABLE; everything reads the VIEW
-- mkt_control.email_template_map (table description).
--
-- Running PART 1 makes nothing sendable: every row is active = FALSE. In the view, `active` is
-- Raivis' approval of the letter's words (MAIN decision A, 2026-09-11), and sendable also needs
-- Brevo isActive; 229-236 are all inactive. Table state read live 2026-09-25 16:15 EEST:
--   welcome_1       35, active TRUE, sendable TRUE -> 229, FALSE. welcome_1 stops being sendable
--                   until 229 is approved: the approval was for 35's words. Track welcome_buyer is
--                   off; 0 welcome_1 rows in this week's assignment. 35 stays in Brevo, unmapped.
--   reorder_2/3, winback_2/3, lost_quarterly: NULL, FALSE -> 230-234, FALSE. Of these, only
--                   lost_quarterly is in this week's assignment (3 589 rows, template NULL).
--   active_xsell    no row -> 235, FALSE. The assignment emits no active_xsell rows, and
--                   track_enabled has no track for it: the row stays inert until the send
--                   engine emits exactly this email_type.
--   akcija_weekly   NOT in PART 1 (see PART 2).
-- The ASSERT stops the apply if any of these rows changed after they were read.

-- PART 1 - 229-235.
ASSERT (
  SELECT COUNTIF(email_type = 'welcome_1' AND template_id = 35 AND active) = 1
     AND COUNTIF(email_type IN ('reorder_2', 'reorder_3', 'winback_2', 'winback_3', 'lost_quarterly')
                 AND template_id IS NULL AND NOT active) = 5
     AND COUNTIF(email_type = 'active_xsell') = 0
  FROM `jaunais-za-aizv04022026.mkt_control.email_template_map_manual`
) AS 'email_template_map_manual changed after 2026-09-25 16:15 EEST - re-read it before applying';

MERGE `jaunais-za-aizv04022026.mkt_control.email_template_map_manual` T
USING (
  SELECT * FROM UNNEST([
    STRUCT('welcome_1' AS email_type, 229 AS template_id, 'money:welcome_1 v1' AS letter),
    ('reorder_2', 230, 'money:reorder_2 v1'),
    ('reorder_3', 231, 'money:reorder_3 v1'),
    ('winback_2', 232, 'money:winback_2 v2'),
    ('winback_3', 233, 'money:winback_3 v2'),
    ('lost_quarterly', 234, 'money:lost_quarterly v2'),
    ('active_xsell', 235, 'money:active_xsell v2')
  ])
) S
ON T.email_type = S.email_type
WHEN MATCHED THEN UPDATE SET
  template_id = S.template_id,
  active = FALSE,
  note = CONCAT('Mapped 2026-09-25 ', IFNULL(CAST(T.template_id AS STRING), 'NULL'), ' -> ',
                CAST(S.template_id AS STRING), ' (', S.letter,
                ', contract v2.7 2154106c033c; MAIN command 6C). active=FALSE until Raivis approves this letter.',
                IF(T.template_id IS NULL, '',
                   CONCAT(' Template ', CAST(T.template_id AS STRING), ' stays in Brevo, unmapped.')),
                ' Previous note: ', IFNULL(T.note, '')),
  updated_at = CURRENT_TIMESTAMP(),
  updated_by = 'Vestulu sabloni (session_01QrczvcDF2eMQ9Hej8sFJ5T) on MAIN command 6C 2026-09-25'
WHEN NOT MATCHED THEN INSERT (email_type, template_id, active, note, updated_at, updated_by)
  VALUES (S.email_type, S.template_id, FALSE,
          CONCAT('New 2026-09-25 -> ', CAST(S.template_id AS STRING), ' (', S.letter,
                 ', contract v2.7 2154106c033c; MAIN command 6C). active=FALSE until Raivis approves this letter.'),
          CURRENT_TIMESTAMP(),
          'Vestulu sabloni (session_01QrczvcDF2eMQ9Hej8sFJ5T) on MAIN command 6C 2026-09-25');

-- PART 2 - akcija_weekly -> 236. Left out of PART 1 on purpose and commented out, so running this
-- file never applies it. The live row carries a guard (Data & analytics, 2026-09-03): "Correctly
-- NULL and must stay NULL: the akcija is a LIST CAMPAIGN sent by Raivis, never a per-person send.
-- Its audience is mkt_control.akcija_residual." The campaign layer takes template_id from the plan,
-- and the plan takes it from this map, so an automatic akcija campaign from 236 needs this row.
-- Writing it lifts that guard. Apply only after MAIN lifts the guard by name.
-- UPDATE `jaunais-za-aizv04022026.mkt_control.email_template_map_manual`
-- SET template_id = 236, active = FALSE,
--     note = CONCAT('Mapped 2026-09-25 NULL -> 236 (money:akcija_weekly v2; MAIN command 6C) after MAIN',
--                   ' lifted the 2026-09-03 guard. active=FALSE until Raivis approves this letter.',
--                   ' Previous note: ', IFNULL(note, '')),
--     updated_at = CURRENT_TIMESTAMP(),
--     updated_by = 'Vestulu sabloni (session_01QrczvcDF2eMQ9Hej8sFJ5T) on MAIN command 6C 2026-09-25'
-- WHERE email_type = 'akcija_weekly' AND template_id IS NULL AND NOT active;

-- PART 3 - later, one letter at a time, only on Raivis' "der N". The template_approval row is
-- written separately.
-- UPDATE `jaunais-za-aizv04022026.mkt_control.email_template_map_manual`
-- SET active = TRUE,
--     note = CONCAT('Approved by Raivis <date> ("der <N>"). ', note),
--     updated_at = CURRENT_TIMESTAMP(), updated_by = '<who, on which MAIN decision>'
-- WHERE email_type = '<email_type>' AND template_id = <N> AND NOT active;

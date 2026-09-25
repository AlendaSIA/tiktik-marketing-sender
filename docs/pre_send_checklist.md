# Pre-send list - engine letters 229-236 (tiktik.lv)

Every item is BLOCKING: no customer send of an engine letter until all of them hold.
Owner of this list: tiktik.lv > Marketing > Vestulu sabloni. Machine part: `presend.py` (exit 0 = nothing blocks).
Started 2026-09-25 on MAIN's command 5.

| # | check | how it is enforced | state 2026-09-25 |
|---|---|---|---|
| 1 | FRESH_LINE_UNGATED - the "jauna partija" line of 232/233/234 sits inside the v2.8 fresh flag (today it is inside `{% if contact.P1_NAME %}` only) | `presend.py` (MAIN command 5: "so it cannot be forgotten") | BLOCKED - live 232/233/234 still unwrapped. v2.8 (d1f506313dc1) names the flag P1_FRESH (P6); presend checks it; branch feat/v2.8-price-fields wraps the line; clears when those templates are in Brevo |
| 2 | PLACEHOLDER_LEFT - no U+27E6 bracket in subject, preheader or HTML (price slots 232-234, weekly theme/offer 236) | `campaign.send_now` refuses (F7, live in e4b8116) and `presend.py` | BLOCKED - live 232/233/234/236 carry tokens; on the branch 232-234 read the v2.8 fields instead, 236 keeps its weekly tokens |
| 3 | Contract v2.8 delivers the price fields and the fresh flag; tokens replaced | sync side (writes the 11 fields) + MAIN | v2.8 issued 25.09; templates prepared on the branch: 232-234 (P2/P4 display, P6), 229-231, 235, 179, 180 (P2 display); fields not written to Brevo yet |
| 4 | Raivis approved the letter ("der N") and a `mkt_control.template_approval` row exists | MAIN | open |
| 5 | Mapping in `mkt_control.email_template_map_manual` for the variant | `sql/email_template_map_229_236.sql` (Vestulu sabloni), applied on MAIN's word, after 4 | prepared 25.09, NOT applied (229-235; 236 waits on the akcija_weekly guard) |
| 6 | Template active in Brevo | MAIN, after 4 | all inactive |
| 7 | Track switched on in `mkt_control.track_enabled` | Raivis / MAIN | 0 of 9 on |
| 8 | The send path is built (`campaign.send_now` still raises "not built") and the day's press approval exists | Sutisanas dzinejs (owner of the send path since 25.09) + MAIN | not built |
| 9 | Slot gates live (179, 180, 232-234 need P1; 235 needs R1) | `bq.PLAN_SQL` in e4b8116 | done |
| 10 | 235 data: R rows already bought (421) or also a D line (68) fixed; co-purchase table refreshed | sync side (issued by MAIN) | open |
| 11 | 236: someone owns the featured page ("Nedelas akcijas") - no weekly refresh since 23.09 | MAIN / shop | open |
| 12 | Draft test all_ok on 3 real contacts of the send week, and the test letter read by Raivis | `draft_test.py` | done 25.09 for 229-236 |
| 13 | 232/233 go only to a contact with a personal price (OFFER_VALID_UNTIL non-empty): their subject and h1 promise one, and a subject cannot switch in the template (the body does: v2.8 P2/P4) | Sutisanas dzinejs (gate) + MAIN | open |

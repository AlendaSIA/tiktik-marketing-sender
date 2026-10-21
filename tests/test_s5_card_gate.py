"""R1 Tuesday chain S5 card gate (MAIN 3 13.10): every rendered FLOW card == cab-<YYYYMMDD>."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import s5_card_gate as G  # noqa: E402
import send_day as SD  # noqa: E402

# the two card shapes blocks.card131 / row_card render (copied from tiktik-marketing-sync blocks.py @ 6295281)
REF = ('<img src="i" alt="{n}" width="150"></a></td></tr><tr><td><div><a href="h">{n}</a></div>'
       '<div><span style="color:#98a2ad;text-decoration:line-through;font-size:13px;">{r}</span> '
       '<span style="color:#12603f;font-weight:bold;font-size:17px;">{p}</span></div>'
       '<div style="color:#12603f;font-size:11px;font-weight:bold;letter-spacing:.5px;">TAVA CENA</div></td></tr></table>')
PLAIN = ('<img src="i" alt="{n}" width="150"></a></td></tr><tr><td><div><a href="h">{n}</a></div>'
         '<div style="color:#1c2b23;font-weight:bold;font-size:15px;">{p}</div></td></tr></table>')


def test_money_formats():
    assert G.money("21,99 €") == 21.99 and G.money("no 4,39 €") == 4.39 and G.money("1 234,50 €") == 1234.5
    assert G.money("") is None and G.money("bez cenas") is None


def test_parse_cards_in_order_with_ref_and_plain_and_escaped_names():
    h = REF.format(n="Cimdi &quot;X&quot; N100", r="10,00 €", p="8,99 €") + PLAIN.format(n="Bahilas", p="no 4,39 €")
    c = G.parse_cards(h)
    assert [x["name"] for x in c] == ['Cimdi "X" N100', "Bahilas"]
    assert c[0]["price"] == 8.99 and c[0]["ref"] == 10.0 and c[1]["price"] == 4.39 and c[1]["ref"] is None


def test_verdict_equal_cab_passes_and_another_price_holds():
    slots = [{"name": "Cimdi", "sku": "65-1", "email_type": "winback_1"}]
    row = {"FLOW_HTML": REF.format(n="Cimdi", r="10,00 €", p="8,99 €")}
    assert G.contact_verdict(row, slots, {("65-1", "winback_1"): 8.99}) is None
    why = G.contact_verdict(row, slots, {("65-1", "winback_1"): 9.49})
    assert why.startswith("S5_CARD_NE_CAB 1:") and "cab 9.49" in why


def test_verdict_cab_row_of_another_letter_type_does_not_vouch():
    slots = [{"name": "Cimdi", "sku": "65-1", "email_type": "winback_1"}]
    row = {"FLOWR_HTML": REF.format(n="Cimdi", r="10,00 €", p="8,99 €")}
    why = G.contact_verdict(row, slots, {("65-1", "active_xsell"): 8.99})
    assert why and "without a cabinet row" in why


def test_verdict_plain_shop_card_without_cab_row_is_not_compared_but_plain_card_with_cab_row_is():
    slots = [{"name": "Bahilas", "sku": "B1", "email_type": "reorder_1"}]
    row = {"FLOW2_HTML": PLAIN.format(n="Bahilas", p="4,39 €")}
    assert G.contact_verdict(row, slots, {}) is None
    assert G.contact_verdict(row, slots, {("B1", "reorder_1"): 4.39}) is None
    assert G.contact_verdict(row, slots, {("B1", "reorder_1"): 3.99}).startswith("S5_CARD_NE_CAB")


def test_flow_order_hold():
    t = lambda h, m: dt.datetime(2026, 10, 20, h, m, tzinfo=dt.timezone.utc)  # noqa: E731
    ok = {"lf_at": t(4, 0), "flow": {"run_id": "blk-1", "finished_at": t(4, 30)}, "table_run": "blk-1"}
    assert G.flow_order_hold(ok) is None
    assert "no OK letter_fields" in G.flow_order_hold({**ok, "lf_at": None})
    assert "no FLOW_HTML build" in G.flow_order_hold({**ok, "flow": None})
    assert "is not after letter_fields" in G.flow_order_hold({**ok, "flow": {"run_id": "blk-1", "finished_at": t(3, 59)}})
    assert "block_fields_today holds" in G.flow_order_hold({**ok, "table_run": "blk-0"})


def test_facts_sql_requires_ok_lf_on_todays_cab_and_flow_build_mode():
    s = G.facts_sql(dt.date(2026, 10, 20))
    assert "status = 'OK'" in s and "'cab-20261020'" in s and "$.cab_ok') = 'true'" in s and "mode = 'LIVE'" in s
    assert "block_fields_today`" in s
    d = G.facts_sql(dt.date(2026, 10, 19), dry=True)
    assert "mode = 'DRY'" in d and "block_fields_today_dry" in d


def test_evaluate_holds_mismatch_per_contact():
    rows = {"facts": [{"lf_at": "2026-10-20 04:00:00", "flow": {"run_id": "blk-1", "finished_at": "2026-10-20 04:30:00"},
                       "table_run": "blk-1"}]}

    def q(sql):
        if "AS lf_at" in sql:
            return rows["facts"]
        if "block_fields_today" in sql:
            return [{"em": "a@x.lv", "run_id": "blk-1", "FLOW_HTML": REF.format(n="Cimdi", r="10,00 €", p="8,99 €")},
                    {"em": "b@x.lv", "run_id": "blk-1", "FLOW_HTML": REF.format(n="Cimdi", r="10,00 €", p="8,99 €")}]
        if "letter_fields_slots" in sql:
            return [{"em": "a@x.lv", "name": "Cimdi", "sku": "65-1", "email_type": "winback_1"},
                    {"em": "b@x.lv", "name": "Cimdi", "sku": "65-1", "email_type": "winback_1"}]
        if "cabinet_price_current" in sql:
            return [{"em": "a@x.lv", "sku": "65-1", "email_type": "winback_1", "price": 8.99},
                    {"em": "b@x.lv", "sku": "65-1", "email_type": "winback_1", "price": 7.99}]
        return []
    r = G.evaluate(q, dt.date(2026, 10, 20), ["A@x.lv", "b@x.lv", "c@x.lv"])
    assert r["order_hold"] is None and r["checked"] == 2 and list(r["holds"]) == ["b@x.lv"]
    assert r["stats"] == {"cards": 2, "vs_cab": 2, "personal": 2}


def test_chain_dry_refused_unless_mode_dry(capsys, monkeypatch):
    monkeypatch.setattr(SD, "_prod", lambda: (lambda s: [], None, None, None, lambda *a: None))
    assert SD.main(["--chain-dry=1", "--date=2026-10-19"]) == 3
    assert "--chain-dry needs --mode=dry" in capsys.readouterr().out


def test_send_day_ok_monday_only_in_chain_dry():
    day = SD.Day.__new__(SD.Day)
    mon = dt.date(2026, 10, 19)
    assert not day.send_day_ok("winback_1", mon)
    day.chain_dry = True
    assert day.send_day_ok("winback_1", mon) and day.send_day_ok("reorder_1", dt.date(2026, 10, 20))


def test_d13_an_e2_slot_is_looked_up_as_its_e1_type_only():
    """D13 (MAIN 6 21.10.2026): winback_r_e2 shows its E1 goods at today's cabinet price of winback_r."""
    _, slots_sql, cab_sql = G.rows_sql(dt.date(2026, 10, 27), ["a@x.lv"])
    assert "REGEXP_REPLACE(email_type, r'_e2$', '') AS email_type" in slots_sql
    assert "REGEXP_REPLACE" not in cab_sql                      # the cabinet keeps its own type
    row = {"FLOW_HTML": REF.format(n="Cimdi", r="4,99 €", p="4,50 €")}
    slots = [{"name": "Cimdi", "sku": "65-1", "email_type": "winback_1"}]      # as the SQL returns an E2 slot
    assert G.contact_verdict(row, slots, {("65-1", "winback_1"): 4.50}) is None
    assert G.contact_verdict(row, slots, {("65-1", "winback_2"): 4.50}) is not None   # another E1 type never vouches
    assert G.contact_verdict(row, slots, {("65-1", "winback_1"): 4.95}) is not None   # an old (E1-day) price never passes

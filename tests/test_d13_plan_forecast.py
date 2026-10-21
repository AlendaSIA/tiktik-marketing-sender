"""D13 F3 (MAIN 6 21.10.2026 11:38): the plan applies D50 and the cabinet half of L7, so would_send = sendable."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sequence as S  # noqa: E402
import send_day as SD  # noqa: E402

D = dt.date(2026, 10, 27)


def test_d50_in_the_plan_matches_the_send_rule():
    assert S.D50_XSELL_MAX_DAYS == SD.D50_XSELL_MAX_DAYS == 30
    assert S.HOLD_D50_XSELL == "D50_XSELL_ORDER_OLD"
    assert S.d50_xsell_hold(S.XSELL, None, D - dt.timedelta(days=30), D) is None
    assert S.d50_xsell_hold(S.XSELL, None, D - dt.timedelta(days=31), D) == "D50_XSELL_ORDER_OLD"
    assert S.d50_xsell_hold(S.XSELL, None, None, D) == "D50_XSELL_ORDER_OLD"            # send path: no last order known
    assert S.d50_xsell_hold("winback_1", None, None, D) is None                          # xsell only
    assert S.d50_xsell_hold(S.XSELL, "EN_PENDING", None, D) == "EN_PENDING"              # an earlier hold stays
    for days in (29, 30, 31, 60):                                                        # the two rules agree day for day
        lo = D - dt.timedelta(days=days)
        send = SD.d50_xsell_refusal({"reason": f"track=x last_order={lo} last_sent=None"}, D)
        assert (S.d50_xsell_hold(S.XSELL, None, lo, D) is None) == (send is None)


def test_cab_row_hold_only_price_letters_only_when_known():
    assert S.cab_row_hold("winback_1", 1, None, False) == "NO_CABINET_ROW"
    assert S.cab_row_hold("winback_1_e2", 1, None, False) == "NO_CABINET_ROW"
    assert S.cab_row_hold("lost_quarterly", 4, None, False) == "NO_CABINET_ROW"
    assert S.cab_row_hold("winback_1", 1, None, True) is None
    assert S.cab_row_hold("winback_1", 1, None, None) is None          # today's cabinet run not ok: the send path decides
    assert S.cab_row_hold("reorder_1", 0, None, False) is None          # not a price letter (negotiated only)
    assert S.cab_row_hold(S.XSELL, 0, None, False) is None
    assert S.cab_row_hold("winback_1", 1, "B2B_FLOW", False) == "B2B_FLOW"


def test_cab_type_of_e2_is_its_e1():
    assert S.cab_type_of("winback_1_e2") == "winback_1" and S.cab_type_of("winback_3_e2") == "winback_3"
    assert S.cab_type_of("winback_2") == "winback_2" and S.cab_type_of("lost_quarterly") == "lost_quarterly"

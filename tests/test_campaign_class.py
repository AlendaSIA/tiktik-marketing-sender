"""D2 (MAIN 4 15.10): engine-created campaigns are classified at creation (campaign_class.py)."""
import datetime as dt
import os
import sys
import zoneinfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import campaign_class as CC  # noqa: E402
import sendlog_sync as SL  # noqa: E402

NOW = dt.datetime(2026, 10, 9, 4, 30, tzinfo=zoneinfo.ZoneInfo("Europe/Riga"))
ROOT = os.path.join(os.path.dirname(__file__), "..")


def test_engine_row_is_reviewed_and_the_nightly_import_never_overwrites_it():
    sql = CC.engine_class_sql(333, "236 2026-10-20 2026-W43 (engine)", "akcija_weekly", "2026-10-20T10:00:00+03:00")
    assert CC.T_CLASS == SL.T_CLASS and f"INSERT INTO `{CC.T_CLASS}`" in sql
    assert "'broadcast', 'akcija_weekly', 'akcija', NULL, NULL, FALSE" in sql
    assert "'engine', CURRENT_TIMESTAMP(), 'auto: engine-created'" in sql
    assert sql.endswith("333 " + CC.GUARD)                   # written twice = one row
    assert CC.GUARD in SL.CLASS_SQL                          # the nightly 'unclassified' insert skips an existing row
    for cls, (kind, et, track, basis) in CC.ENGINE_CLASSES.items():
        s = CC.engine_class_sql(1, "x", cls, "t")
        assert f"'{kind}'" in s and f"'{track}'" in s and "FALSE" in s, cls
    assert [CC.ENGINE_CLASSES[k][0] for k in ("akcija_weekly", "akcija_test", "edu", "edu_test")] == \
        ["broadcast", "test", "edu", "test"]


def test_engine_class_never_raises_and_escapes():
    seen = []
    assert CC.engine_class(lambda s: seen.append(s), 7, "it's a\\b", "edu", NOW) is None
    assert "'it\\'s a\\\\b'" in seen[0] and "'2026-10-09T04:30:00+03:00'" in seen[0]

    def boom(s):
        raise RuntimeError("BigQuery refused")
    assert CC.engine_class(boom, 7, "x", "edu", NOW).startswith("CLASS_ROW_FAILED RuntimeError: BigQuery refused")
    assert CC.engine_class(lambda s: None, 7, "x", "no_such", NOW).startswith("CLASS_ROW_FAILED KeyError")


def test_module_writes_only_the_class_table_and_sends_nothing():
    src = open(os.path.join(ROOT, "campaign_class.py"), encoding="utf-8").read().split('"""', 2)[2]
    for word in ("send_log", "sendNow", "/smtp/", "POST", "UPDATE ", "DELETE", "contact_sequence"):
        assert word not in src, word
    assert src.count("INSERT INTO") == 1

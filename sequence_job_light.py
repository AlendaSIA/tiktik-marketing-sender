"""The pure helpers of sequence_job, importable without the BigQuery client (tools/d1_trace.py on a bare shell)."""
import sys
import types

if "google.cloud.bigquery" not in sys.modules:
    try:
        import google.cloud.bigquery  # noqa: F401
    except ImportError:
        g, gc = types.ModuleType("google"), types.ModuleType("google.cloud")
        bqm = types.ModuleType("google.cloud.bigquery")
        g.cloud, gc.bigquery = gc, bqm
        sys.modules.update({"google": g, "google.cloud": gc, "google.cloud.bigquery": bqm})
from sequence_job import akcija_row, akcija_week, order_nr_of, order_nr_usable, pp_facts  # noqa: E402,F401

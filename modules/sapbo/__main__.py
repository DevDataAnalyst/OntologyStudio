"""
CLI for the SAP BO POC pipeline (no Databricks needed for the deterministic stages).

  python -m modules.sapbo run   [BUNDLE] -o runs/cnr       # bundle folder or .zip; default: synthetic sample
  python -m modules.sapbo fixture samples/sapbo/cnr_finance # regenerate the synthetic bundle
  python -m modules.sapbo recon [BUNDLE] --profile <databricks-cli-profile> --warehouse <id>
"""
from __future__ import annotations

import argparse
import json
import sys

from . import bundle, fixture, pipeline, recon


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.sapbo")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="extract bundle -> SIR -> Ossie -> metric views (+ reports)")
    r.add_argument("bundle", nargs="?")
    r.add_argument("-o", "--out", default="runs/sapbo")
    r.add_argument("--catalog", default="cnfinance")
    r.add_argument("--schema", default="semantic")
    r.add_argument("--no-report-metrics", action="store_true")
    f = sub.add_parser("fixture", help="write the synthetic CNR Finance bundle")
    f.add_argument("dir", nargs="?", default="samples/sapbo/cnr_finance")
    c = sub.add_parser("recon", help="run the reconciliation queries on a SQL warehouse")
    c.add_argument("bundle", nargs="?")
    c.add_argument("--profile", required=True, help="Databricks CLI / SDK profile")
    c.add_argument("--warehouse", required=True)
    c.add_argument("--catalog", default="cnfinance")
    c.add_argument("--schema", default="semantic")
    a = ap.parse_args(argv)

    if a.cmd == "fixture":
        print(fixture.write(a.dir))
        return 0
    b = bundle.load(a.bundle) if a.bundle else bundle.sample()
    res = pipeline.run(b, a.catalog, a.schema, report_metrics=not getattr(a, "no_report_metrics", False))
    if a.cmd == "run":
        out = pipeline.write_run(res, a.out)
        print(json.dumps({"out": str(out), "coverage_pct": res["coverage"]["pct"],
                          "ossie": res["ossie_validation"]["headline"], "metric_views": list(res["mv"]["views"]),
                          "provenance": res["provenance"]}, indent=2))
        return 0
    from databricks.sdk import WorkspaceClient
    import pandas as pd
    w = WorkspaceClient(profile=a.profile)

    def run_sql(q):
        resp = w.statement_execution.execute_statement(statement=q, warehouse_id=a.warehouse, wait_timeout="50s")
        if resp.status.state.value != "SUCCEEDED":
            raise RuntimeError(resp.status.error.message if resp.status.error else resp.status.state.value)
        cols = [x.name for x in resp.manifest.schema.columns]
        return pd.DataFrame(resp.result.data_array or [], columns=cols)
    rows = recon.run(res["recon_plan"], run_sql)
    for row in rows:
        print(json.dumps(row, default=str))
    print(json.dumps(recon.summary(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

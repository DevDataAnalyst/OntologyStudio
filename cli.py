"""
Command-line entry point (same engine as the app; runs anywhere, no Databricks needed).

  python cli.py path/to/database.db  -o shop.ontology.yaml
  python cli.py path/to/schema.sql   --dialect mysql --name Shop --infer
"""
import argparse
import pathlib
import sys

from modules.extractors import from_sql_script, from_sqlite_file, is_sqlite_bytes
import os
import sqlite3
import tempfile

import pandas as pd

from modules import discovery, metric_views as mv, semantics
from modules.ontology_builder import (BuildOptions, attach_suggestions, build_ontology, to_yaml,
                                      validate)


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a YAML ontology from a SQL database file")
    ap.add_argument("input", help=".db/.sqlite file or .sql script")
    ap.add_argument("-o", "--output", help="output .yaml (default: stdout)")
    ap.add_argument("--dialect", default="auto")
    ap.add_argument("--name", default=None, help="ontology name (default: file name)")
    ap.add_argument("--base-iri", default="http://example.org/ontology/")
    ap.add_argument("--prefix", default="ex")
    ap.add_argument("--raw-names", action="store_true", help="keep SQL names as-is")
    ap.add_argument("--infer", action="store_true", help="infer FKs from *_id column names")
    ap.add_argument("--no-profile", action="store_true")
    ap.add_argument("--keep-junctions", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.75,
                    help="auto-accept discovered joins at/above this confidence")
    ap.add_argument("--metric-views", metavar="DIR", help="also write Databricks metric view YAMLs here")
    a = ap.parse_args()

    p = pathlib.Path(a.input)
    data = p.read_bytes()
    prof = not a.no_profile
    db = (from_sqlite_file(data, p.stem, profile=prof) if is_sqlite_bytes(data)
          else from_sql_script(data.decode("utf-8-sig", "replace"), p.stem, a.dialect, profile=prof))
    # relationship discovery (value-overlap checks run when the input is SQLite-executable)
    run_sql = table_ref = quote = None
    if db.dialect == "sqlite":
        path = os.path.join(tempfile.gettempdir(), f"cli_{p.stem}.db")
        if is_sqlite_bytes(data):
            path = str(p)
        else:
            if os.path.exists(path):
                os.remove(path)
            c = sqlite3.connect(path)
            c.executescript(data.decode("utf-8-sig", "replace"))
            c.close()
        conn = sqlite3.connect(path)
        run_sql = lambda q: pd.read_sql_query(q, conn)  # noqa: E731
        table_ref = lambda t: '"' + t.name + '"'  # noqa: E731
        quote = lambda col: '"' + col + '"'  # noqa: E731
    suggestions, _ = discovery.discover(db, run_sql=run_sql, table_ref=table_ref, quote=quote,
                                        verify=run_sql is not None, threshold=a.threshold)
    opts = BuildOptions(ontology_name=a.name or p.stem.title().replace("_", ""),
                        base_iri=a.base_iri, prefix=a.prefix,
                        naming="raw" if a.raw_names else "semantic",
                        infer_relationships=a.infer, enum_detection=prof,
                        collapse_junction_tables=not a.keep_junctions)
    onto = build_ontology(db, opts)
    attach_suggestions(onto, suggestions)
    semantics.classify(onto)
    out = to_yaml(onto)
    if a.metric_views:
        os.makedirs(a.metric_views, exist_ok=True)
        for fact, view in mv.build_all(onto).items():
            fn = os.path.join(a.metric_views, f"{mv.view_name(onto, fact)}.yaml")
            pathlib.Path(fn).write_text(mv.to_yaml(view), encoding="utf-8")
            print(f"Wrote {fn}", file=sys.stderr)
    if a.output:
        pathlib.Path(a.output).write_text(out, encoding="utf-8")
        print(f"Wrote {a.output}: {onto['ontology']['stats']}", file=sys.stderr)
    else:
        print(out)
    for issue in validate(onto) + onto.get("warnings", []):
        print("note:", issue, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

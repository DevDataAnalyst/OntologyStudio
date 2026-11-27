"""
SAP BusinessObjects → Apache Ossie → Databricks metric views → Power BI (POC pipeline).

Stages (each one is a plain function over JSON-able dicts, so the app, the CLI and the tests share them):

  bundle.load_*          extract bundle (Universe + Webi reports, as captured from BO)   [WS1]
  parse.to_sir           bundle → Semantic IR (SIR): physical, semantic, reports        [WS2]
  normalize.run          SIR → concepts (datasets, relationships, fields, metrics,
                         filters, time calcs, variants) + findings                      [WS2]
  gaps.*                 gap report, required artifacts, extraction coverage            [WS1/WS2]
  enrich.*               LLM proposals with provenance, human review                    [WS3]
  to_ossie.build         concepts → Apache Ossie 0.3.0.dev0 (SAP_BO extensions)         [WS4]
  to_mv.convert          Ossie → metric view YAML 1.1 + DDL + conversion report         [WS5]
  consume.*              Power BI pattern helpers: SQL views (P2), DAX/TMDL (P1),
                         DAX classifier (P3)                                            [WS6]
  recon.*                reconciliation queries and matrix                              [WS7]

`pipeline.run(bundle)` runs everything up to the metric views.
See plans/sap-bo-ossie-powerbi-poc-roadmap.md for the full roadmap.
"""

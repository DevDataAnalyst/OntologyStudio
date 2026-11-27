# SAP BusinessObjects extract bundle

The SAP BO workspace (Connect → Non-Databricks → **SAP BusinessObjects**) reads an *extract bundle*: a folder or `.zip`
holding what an extractor captured from BusinessObjects. The app never connects to a BO server itself; run the
extractor where BO is reachable and upload the bundle. The synthetic **CNR Finance** bundle in
[`samples/sapbo/cnr_finance`](../../samples/sapbo/cnr_finance) shows every file (regenerate it with
`python -m modules.sapbo fixture samples/sapbo/cnr_finance`).

```
manifest.json          bundle_version, source_system, extracted_at, extractor, universe, reports[], extraction_methods
universe.json          the Universe: connection, dataFoundation, businessLayer
mapping.json           BO table -> Unity Catalog table
reports/R01.json ...   one Web Intelligence document per file
```

## universe.json

| Key | Holds | Typical source |
|---|---|---|
| `connection` | `name`, `dialect` (e.g. `oracle`), `qualifier` (owner/schema) | IDT connection |
| `dataFoundation.tables` | tables, aliases (`alias_of`) and derived tables (`sql`), with columns and types | Semantic Layer Java SDK (the REST API doesn't expose the data foundation) |
| `dataFoundation.joins` | join expression, cardinality (may be missing), shortcut/theta flags | SDK |
| `dataFoundation.contexts` | context name and the joins it includes | SDK |
| `businessLayer.items` | folders, dimensions, attributes, measures and filters with `select`/`where` SQL, projection function, `@Aggregate_Aware`, `@Prompt`, `@Select`/`@Where` | `/biprws/sl/v1/universes/{id}` |
| `businessLayer.parameters` | prompts (`@Prompt` definitions) | `/biprws/sl/v1` |
| `businessLayer.hierarchies` | navigation paths | `/biprws/sl/v1` |
| `businessLayer.securityProfiles` | row restrictions (captured, then flagged; never converted) | CMC / SDK |

## reports/Rxx.json

| Key | Holds | Typical source |
|---|---|---|
| `usage` | refresh counts used to pick the top 10 | Audit DB / CMS |
| `dataProviders[]` | `querySpecification` (result objects, conditions, predefined filters), `customSql`, `promptValues` and the generated `queryPlan` SQL (one statement per context) | `/biprws/raylight/v1/documents/{id}/dataproviders/{dp}` and `/queryplan` |
| `variables[]` | report variables: `qualification` and Webi `formula` | `/biprws/raylight/v1/documents/{id}/variables` |
| `reports[].blocks[]` | tables and charts: axes (dimensions) and measures | `/biprws/raylight/v1/documents/{id}/reports/{r}/elements` |

## mapping.json

`{"tables": [{"bo_table", "uc_table", "uc_columns": {column: type}, "uc_primary_key": [...]}]}`. A table with
`uc_table: null` is reported as **unmapped** and every object on it becomes a gap. In the POC the mapping is written
by hand (or from the migration's table-mapping sheet); it's the only input that isn't extracted.

## What happens to it

1. **Parse** into a source-neutral intermediate (SIR) with extraction coverage per report (target ≥ 95% of what the
   reports need).
2. **Normalize** into datasets, relationships, fields, metrics, filters and parameters; every property carries a
   provenance state (Extracted, Derived, LLM-proposed, Human-approved/edited/rejected, Needs human, Unsupported).
3. **Enrich** (optional, in Databricks Apps): the LLM proposes descriptions and synonyms only; a reviewer approves,
   edits or rejects each one.
4. **Ossie**: canonical Apache Ossie YAML, BO specifics kept in a `SAP_BO` extension.
5. **Metric views**: Ossie → Databricks metric view YAML 1.1 with a per-element conversion status; `deploy.sql` for
   a manual deployment.
6. **Power BI** helpers (DAX/TMDL, SQL views per report block, a DAX extension-vs-override classifier) and
   **reconciliation** SQL that compares each BO statement with the same question asked of the metric view.

The same flow runs from the command line: `python -m modules.sapbo run [bundle] -o runs/cnr`.

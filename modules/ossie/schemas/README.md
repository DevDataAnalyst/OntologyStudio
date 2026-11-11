# Apache Ossie schemas (bundled)

These JSON Schemas come unchanged from the [Apache Ossie](https://github.com/apache/ossie)
repository and are distributed under the Apache License 2.0 (see `LICENSE` and `NOTICE`
in this folder). Ontology Studio validates each uploaded document against the schema of the
version the document declares.

| File | Declared `version` | Source |
|------|--------------------|--------|
| `ossie-schema-0.3.0.dev0.json` | `0.3.0.dev0` (current development version) | `core-spec/ossie-schema.json` at commit `e6ad94ee87a53fd8f8b6dbf0de33fab59e406c0c` (2026-11-11) |
| `ossie-schema-0.2.0.dev0.json` | `0.2.0.dev0` (former development label) | `core-spec/ossie-schema.json` at commit `21e849da3bc207c03e79efa2a0f21b325fe3bf86` (2026-11-03), the last schema published under this label |
| `osi-schema-0.1.1.json` | `0.1.1` (first release, 2025-12-11) | `core-spec/osi-schema.json` at tag `osi-0.1.1-rc1` (commit `faf581054dcf7964d5fe0ceae7d6f415c8ce32a5`) |

The semantic checks in `../validator.py` follow the reference validator in the same
repository (`validation/validate.py`). To refresh a schema, download the file at a newer
commit, keep the name pattern, and update the table above and `PROFILES` in `validator.py`.

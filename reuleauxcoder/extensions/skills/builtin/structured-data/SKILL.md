---
name: structured-data
metadata:
  rcoder.display-name: "Structured data"
  rcoder.display-name.zh-CN: "结构化数据"
  rcoder.summary: "Inspect and transform JSON, JSONL and CSV"
  rcoder.summary.zh-CN: "检查和转换 JSON、JSONL 与 CSV"
  rcoder.icon: "table"
  rcoder.category: "documents"
description: Query, transform and validate JSON, JSONL and CSV with explicit type, encoding and size handling.
---

# Process structured data

Inspect a small sample and establish format, field meanings, output contract and scale. Do not turn string IDs into numbers, missing fields into nulls or empty strings into zero. Preserve meaningful leading zeroes, timezones and integer precision. Validate configuration against its schema.

## Choose the implementation

- Use real `jq` if installed. rcoder does not embed gojq or guarantee its extensions. Pass user values through files or `--arg`/`--argjson`, never as interpolated filter code.
- Python's standard library handles JSON, JSONL and CSV. Stream JSONL/CSV; for a JSON document exceeding memory limits, use an available streaming parser. Avoid dumping sensitive datasets.
- Use `csv` for quoting, embedded newlines, delimiters and BOMs, not `split(',')`. Use `decimal.Decimal` where exact decimal arithmetic matters and define serialization explicitly.
- CSV has no cell types. For spreadsheet-bound, user-controlled fields starting with `= + - @`, choose text-cell output or an agreed escaping policy; do not silently corrupt the source data. Workbook layout and formula authoring are outside this skill.

## Validate and deliver

Try transformations on a small representative sample, then process the full input. Check row counts, required fields, types, duplicate keys/identifiers, encoding and rejected records. Explain intentional filtering.

Python JSON defaults accept some nonstandard numeric values; use `allow_nan=False` for strict output and reject duplicate keys when correctness requires it.

Write to a new file or the explicitly selected destination, then reopen and parse it. Preserve source files. Establish batch failure behavior; an empty output is not proof of success. Report transformation rules, counts and failed-record locations without exposing sensitive records.

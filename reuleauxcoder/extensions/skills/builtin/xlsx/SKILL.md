---
name: xlsx
metadata:
  rcoder.display-name: "Spreadsheets"
  rcoder.display-name.zh-CN: "电子表格"
  rcoder.summary: "Build workbooks and check formulas and calculated results"
  rcoder.summary.zh-CN: "制作工作簿，检查公式与计算结果"
  rcoder.icon: "table"
  rcoder.category: "documents"
description: Create, edit and analyze Excel workbooks with formulas, charts, explicit data types and calculation checks.
---

# Excel workbooks

Identify the requested workbook, sheets, source data, calculations and downstream use. Keep identifiers as text, clarify units/currencies and distinguish blank from zero. Preserve supplied files and deliver a new workbook unless replacement was requested.

Use `openpyxl` in a task environment for ordinary `.xlsx` work. It writes formulas but does not calculate them. Detect required dependencies on the execution host; use LibreOffice or another available, suitable spreadsheet application for actual recalculation. Do not install a heavy application for a task that only reads cells.

- For creation, data analysis and formatting, read [references/workbooks.md](references/workbooks.md).
- For recalculation, PDF preview and existing-file fidelity, read [references/calculation.md](references/calculation.md).
- Run [scripts/check_workbook.py](scripts/check_workbook.py) `<file.xlsx>` to inspect sheets, formulas, error cells and missing cached results. After recalculation, use `--require-cache` when formula results must be available to readers.

Resolve resources inside this skill's directory. No other skill or proprietary spreadsheet runtime is required.

## Verify the result

Check source/output row counts, headers, data types, missing/duplicate records, totals and representative formulas. Reopen both formula and cached-value views; cached results can be stale even when present. Recalculate with a compatible engine when calculations are part of the deliverable.

Inspect formatting in a spreadsheet application or rendered preview: widths, wrapped text, filters, frozen headers, number formats and charts. A structural pass cannot prove formula correctness or visual readability. Report which calculation engine and visual checks ran, and distinguish unchecked formulas from verified values.

# Build and analyze workbooks

Organize sheets by purpose: source data, calculations and a reader-facing summary where useful. Keep formulas traceable to input cells, label units, and avoid unexplained hardcoded constants. Use tables/filters and frozen headers for large data regions.

```python
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

output = Path('analysis.xlsx')
if output.exists():
    raise FileExistsError(output)
book = Workbook()
sheet = book.active
sheet.title = 'Summary'
sheet.append(['Item', 'Quantity', 'Unit price', 'Total'])
sheet.append(['Example', 3, 12.5, '=B2*C2'])
sheet.freeze_panes = 'A2'
sheet.auto_filter.ref = 'A1:D2'
for cell in sheet[1]:
    cell.font = Font(bold=True, color='FFFFFF')
    cell.fill = PatternFill('solid', fgColor='264653')
for column in ('A', 'B', 'C', 'D'):
    sheet.column_dimensions[column].width = 18
sheet['C2'].number_format = sheet['D2'].number_format = '#,##0.00'
book.save(output)
```

The saved formula has no calculated cache until a spreadsheet engine processes it. Do not replace formulas with pasted values just to make a validation check pass.

Use actual numeric/date cells with appropriate display formats. Keep IDs, phone numbers and codes as strings. For user-controlled text beginning with a formula marker, explicitly store a text cell; inspect the reopened type to ensure it is not executable formula content. CSV input needs a real CSV parser for quoting, newlines and BOM handling.

Charts should reference actual ranges, name axes/units and convey a useful comparison. Avoid 3D effects and colors that are the only carrier of meaning. Apply conditional formatting to meaningful thresholds, not arbitrary decoration.

For analysis, state filters, handling of missing values, denominators and aggregation rules. Check a few calculations independently. Preserve source data and make intentional exclusions visible.

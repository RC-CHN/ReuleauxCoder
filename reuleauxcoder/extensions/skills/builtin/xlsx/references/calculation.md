# Recalculate and preserve fidelity

Read an existing workbook with formulas intact (`data_only=False`). Read a second instance with `data_only=True` to inspect stored results. Never save the value-only view over a formula workbook. Cached values may be missing or stale; openpyxl does not evaluate formulas.

For `.xlsm`, `keep_vba=True` preserves the VBA payload when supported, but does not validate or execute it. Embedded controls, slicers, external links, unusual charts and other advanced features may not survive a library round trip. Determine whether a compatible application or targeted edit is required before saving.

Recalculate into a new directory with a known spreadsheet engine. Example using an installed LibreOffice executable:

```python
from pathlib import Path
import subprocess
import tempfile

source = Path('analysis.xlsx').resolve()
out = Path('recalculated').resolve()
out.mkdir(exist_ok=False)
with tempfile.TemporaryDirectory() as work:
    subprocess.run([
        'soffice', f'-env:UserInstallation={(Path(work) / "profile").as_uri()}',
        '--headless', '--convert-to', 'xlsx', '--outdir', str(out), str(source),
    ], check=True, timeout=120)
result = out / source.name
if not result.is_file() or not result.stat().st_size:
    raise RuntimeError('Recalculated workbook was not produced')
```

Use the actual executable path on Windows/macOS if it is not on PATH. Reopen the result, compare sheets/formulas and check cached values with `scripts/check_workbook.py --require-cache` from this skill. Check representative results independently; a conversion may alter formulas or unsupported features.

For visual previews, define print area, orientation and scaling deliberately, convert with `--convert-to pdf` to a fresh directory, then render that PDF with an available renderer such as PyMuPDF. View the pages. A tiny one-page export of a wide table is not readable; consider landscape, repeated headers or multiple pages. State the engine used and target-application limitations.

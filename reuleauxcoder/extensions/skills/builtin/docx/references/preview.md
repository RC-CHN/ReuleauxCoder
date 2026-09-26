# Convert and inspect pages

Find LibreOffice on the execution host (`soffice`, or its actual Windows/macOS executable path). Run with argv arrays, a timeout and a private temporary profile. Use a new output directory to avoid confusing stale output with a successful conversion.

```python
from pathlib import Path
import subprocess
import tempfile

source = Path('report.docx').resolve()
preview = Path('report-preview').resolve()
preview.mkdir(exist_ok=False)
with tempfile.TemporaryDirectory() as work:
    subprocess.run([
        'soffice', f'-env:UserInstallation={(Path(work) / "profile").as_uri()}',
        '--headless', '--convert-to', 'pdf', '--outdir', str(preview), str(source),
    ], check=True, timeout=120)
pdf = preview / (source.stem + '.pdf')
if not pdf.is_file() or not pdf.stat().st_size:
    raise RuntimeError('Expected PDF was not generated')
```

For legacy `.doc`, use a separate conversion directory and `--convert-to docx`, then inspect the converted file before editing. To review PDF pages, use an available renderer such as PyMuPDF:

```python
import pymupdf

with pymupdf.open(pdf) as document:
    for number, page in enumerate(document, 1):
        page.get_pixmap(dpi=120).save(preview / f'page-{number:03}.png')
```

View the images. Inspect title/section starts, tables spanning pages, page numbers, headers/footers, whitespace and language coverage. LibreOffice and Word can paginate differently; state the renderer used and any untested target-application behavior.

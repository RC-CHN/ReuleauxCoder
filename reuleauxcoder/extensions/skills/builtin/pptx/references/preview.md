# Render and inspect slides

Locate `soffice`/LibreOffice on the execution host. On Windows or macOS it may be installed outside PATH. Use argv arrays, a timeout, a new output directory and a unique temporary user profile so an existing GUI instance does not absorb the conversion.

```python
from pathlib import Path
import subprocess
import tempfile

source = Path('presentation.pptx').resolve()
preview = Path('presentation-preview').resolve()
preview.mkdir(exist_ok=False)
with tempfile.TemporaryDirectory() as work:
    profile = (Path(work) / 'profile').as_uri()
    subprocess.run([
        'soffice', f'-env:UserInstallation={profile}', '--headless',
        '--convert-to', 'pdf', '--outdir', str(preview), str(source),
    ], check=True, timeout=120)
pdf = preview / (source.stem + '.pdf')
if not pdf.is_file() or not pdf.stat().st_size:
    raise RuntimeError('LibreOffice did not produce the expected PDF')
```

To inspect slides as images, use an available PDF renderer, for example:

```python
import pymupdf

with pymupdf.open(pdf) as document:
    for index, page in enumerate(document, 1):
        page.get_pixmap(dpi=120).save(preview / f'slide-{index:03}.png')
```

Actually open the resulting images with an available image-viewing tool. Verify slide count and view individual slides, not just a tiny contact sheet. Font substitution and unsupported features can differ between LibreOffice and PowerPoint; record which renderer was tested.

# Author a structured document

Choose sections based on the reader's task, then define heading and body styles. Use a table of contents only if document length warrants it. Page size, margins, headers/footers and numbering should support the intended print or screen use.

Example using `python-docx`:

```python
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt

output = Path('report.docx')
if output.exists():
    raise FileExistsError(output)
doc = Document()
section = doc.sections[0]
section.top_margin = section.bottom_margin = Inches(0.8)
normal = doc.styles['Normal']
normal.font.name = 'DejaVu Sans'  # Select a suitable installed font.
normal.font.size = Pt(11)
doc.add_heading('Report title', 0)
doc.add_heading('Findings', 1)
doc.add_paragraph('A verified finding, with the evidence needed by the reader.')
table = doc.add_table(rows=1, cols=2)
table.style = 'Table Grid'
table.rows[0].cells[0].text = 'Measure'
table.rows[0].cells[1].text = 'Value'
row = table.add_row().cells
row[0].text, row[1].text = 'Sample count', '12'
doc.save(output)
```

Use paragraph spacing, keep-with-next for headings and appropriate page breaks instead of repeated newlines. For CJK text, configure the East Asian font in run/style XML when required and verify the renderer has it. Tables need readable column widths and header rows; repeated headings, page-number fields and contents fields may require Word/LibreOffice updates before final rendering.

Link evidence or include actual source notes where the document needs them. Do not fabricate names, citations or numbers. Keep tables editable unless the user requests a visual-only deliverable.

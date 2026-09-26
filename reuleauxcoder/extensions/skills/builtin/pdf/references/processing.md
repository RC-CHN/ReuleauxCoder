# Process an existing PDF

Use a task environment containing `pypdf`; rendering additionally needs `PyMuPDF`. The helper has no installation side effects and refuses to replace existing output files/directories. Page numbers are one-based, and a selection such as `1,3-5` preserves the requested order.

```text
python <skill-dir>/scripts/pdf_tools.py info input.pdf
python <skill-dir>/scripts/pdf_tools.py extract input.pdf extracted.txt
python <skill-dir>/scripts/pdf_tools.py merge combined.pdf first.pdf second.pdf
python <skill-dir>/scripts/pdf_tools.py select input.pdf selected.pdf --pages 1,3-5
python <skill-dir>/scripts/pdf_tools.py render input.pdf preview --dpi 120 --pages 1-3
```

`info` reports text availability per page. An empty result may be a blank page, a scan or extraction failure; inspect before deciding. `extract` writes UTF-8 with page separators; it is not a table-layout or reading-order guarantee. Use positional extraction or table-specific tooling when columns matter.

For scans, use an available OCR engine in the correct language and keep the original image as evidence. Check names, numbers and low-confidence regions manually; never invent unreadable content. OCR introduces additional dependencies and is not built into this helper.

The helper deliberately rejects encrypted PDFs. Obtain an authorized decrypted working copy through a suitable application if necessary; do not print passwords. Merge/select preserve ordinary page content but may not preserve document signatures, bookmarks, tagged-PDF structure or complex interactive forms. Check these features explicitly when required.

Filling form fields requires identifying actual field names, handling appearance streams and checking the result in a reader. Do not claim that a field's stored value is visually displayed without checking.

For redaction, covering text with a rectangle is insufficient: underlying text/images may remain recoverable. Use an actual redaction operation and verify extraction and visible output. PDF signatures are invalidated by edits; preserve the signed original.

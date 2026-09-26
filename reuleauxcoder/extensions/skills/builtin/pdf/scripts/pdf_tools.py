"""Small PDF operations with explicit outputs and no implicit overwrite."""

import argparse
import json
from pathlib import Path
import sys


def page_indices(spec: str | None, count: int) -> list[int]:
    if spec is None:
        return list(range(count))
    result = []
    for token in spec.split(","):
        bounds = token.strip().split("-")
        if not 1 <= len(bounds) <= 2:
            raise ValueError("Pages must be one-based numbers or ascending ranges")
        first = int(bounds[0])
        last = int(bounds[-1])
        if not 1 <= first <= last <= count:
            raise ValueError(f"Page selection outside 1..{count}: {token}")
        result.extend(range(first - 1, last))
    return result


def run(args) -> dict:
    if args.operation == "render":
        import pymupdf

        if not 36 <= args.dpi <= 300:
            raise ValueError("dpi must be between 36 and 300")
        with pymupdf.open(args.input) as document:
            if document.needs_pass:
                raise ValueError("Encrypted PDF: provide an authorized decrypted working copy")
            indices = page_indices(args.pages, len(document))
            args.output.mkdir()  # Refuse existing directories, including symlinks.
            for sequence, index in enumerate(indices, 1):
                document[index].get_pixmap(dpi=args.dpi).save(args.output / f"{sequence:03}-page-{index + 1}.png")
        return {"rendered_pages": len(indices), "output": str(args.output)}

    from pypdf import PdfReader, PdfWriter

    def read(path):
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF: provide an authorized decrypted working copy")
        return reader

    if args.operation == "merge":
        writer = PdfWriter()
        for source in args.inputs:
            writer.append(read(source))
        with args.output.open("xb") as output:
            writer.write(output)
        return {"pages": len(writer.pages), "output": str(args.output)}

    reader = read(args.input)
    if args.operation == "select":
        indices = page_indices(args.pages, len(reader.pages))
        writer = PdfWriter()
        for index in indices:
            writer.add_page(reader.pages[index])
        with args.output.open("xb") as output:
            writer.write(output)
        return {"pages": [index + 1 for index in indices], "output": str(args.output)}

    if args.operation == "info":
        return {
            "pages": len(reader.pages),
            "text_characters": [len(page.extract_text() or "") for page in reader.pages],
            "note": "Text absence may indicate scans or blank pages; no OCR or layout validation performed.",
        }
    with args.output.open("x", encoding="utf-8") as output:
        for index, page in enumerate(reader.pages):
            if index:
                output.write("\n\f\n")
            output.write(page.extract_text() or "")
    return {"pages": len(reader.pages), "output": str(args.output), "ocr_performed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("info", "extract", "select", "render"):
        command = operations.add_parser(name)
        command.add_argument("input", type=Path)
        if name != "info":
            command.add_argument("output", type=Path)
        if name in ("select", "render"):
            command.add_argument("--pages", required=name == "select")
        if name == "render":
            command.add_argument("--dpi", type=int, default=120)
    merge = operations.add_parser("merge")
    merge.add_argument("output", type=Path)
    merge.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        result = run(args)
    except ImportError as exc:
        print(f"Missing dependency {exc.name}: install pypdf for processing or PyMuPDF for rendering in the task environment.", file=sys.stderr)
        return 2
    except Exception as exc:
        # This CLI reports third-party parser/renderer errors without claiming success.
        print(f"PDF operation failed: {exc}. Any partially written output is incomplete.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

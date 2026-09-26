"""Inventory Word XML without changing revisions, comments or document contents."""

import argparse
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
import zipfile


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def inspect(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        document = ET.fromstring(archive.read("word/document.xml"))
        paragraphs = []
        for paragraph in document.findall(".//w:body//w:p", NS):
            style = paragraph.find("w:pPr/w:pStyle", NS)
            paragraphs.append({
                "text": "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)),
                "style": style.get(f"{{{NS['w']}}}val") if style is not None else None,
            })
        revisions = {"insertions": 0, "deletions": 0, "comment_anchors": 0}
        for name in archive.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                root = ET.fromstring(archive.read(name))
                for key, tag in (("insertions", "ins"), ("deletions", "del"), ("comment_anchors", "commentRangeStart")):
                    revisions[key] += len(root.findall(f".//w:{tag}", NS))
        comments = []
        if "word/comments.xml" in archive.namelist():
            root = ET.fromstring(archive.read("word/comments.xml"))
            comments = [
                {"id": node.get(f"{{{NS['w']}}}id"), "text": "".join(node.itertext())}
                for node in root.findall("w:comment", NS)
            ]
        return {
            "paragraphs": paragraphs,
            "table_count": len(document.findall(".//w:tbl", NS)),
            "revisions": revisions, "comments": comments,
            "not_checked": ["pagination", "visual_layout", "resolved_revision_text", "application_compatibility"],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        result = inspect(args.file)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"Cannot inspect document: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

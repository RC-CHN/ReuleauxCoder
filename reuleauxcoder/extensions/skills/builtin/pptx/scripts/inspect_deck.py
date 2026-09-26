"""Read slide content and flag top-level geometry outside the slide canvas."""

import argparse
import json
from pathlib import Path
import sys


def inspect(path: Path) -> dict:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    deck = Presentation(path)
    slides, warnings = [], []

    def shapes(items, slide_number, grouped=False):
        result = []
        for shape in items:
            item = {"name": shape.name, "type": str(shape.shape_type)}
            if shape.has_text_frame:
                item["text"] = shape.text_frame.text
            if shape.has_table:
                item["table"] = [[cell.text for cell in row.cells] for row in shape.table.rows]
            if shape.has_chart:
                item["chart"] = str(shape.chart.chart_type)
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                item["children"] = shapes(shape.shapes, slide_number, grouped=True)
            # Child coordinates belong to a group, not directly to the slide.
            if not grouped and (
                shape.left < 0 or shape.top < 0
                or shape.left + shape.width > deck.slide_width
                or shape.top + shape.height > deck.slide_height
            ):
                warnings.append({"slide": slide_number, "shape": shape.name, "issue": "outside_canvas"})
            result.append(item)
        return result

    for number, slide in enumerate(deck.slides, 1):
        item = {"number": number, "shapes": shapes(slide.shapes, number)}
        if slide.has_notes_slide:
            frame = slide.notes_slide.notes_text_frame
            item["notes"] = frame.text if frame is not None else ""
        slides.append(item)
    return {
        "slide_count": len(slides), "width_emu": deck.slide_width, "height_emu": deck.slide_height,
        "slides": slides, "warnings": warnings,
        "not_checked": ["text_fit", "overlap", "visual_design", "target_application_rendering"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        result = inspect(args.file)
    except ImportError:
        print("Missing dependency: install python-pptx in the task Python environment.", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"Cannot inspect presentation: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

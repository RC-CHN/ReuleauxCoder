# Create an editable deck

Use `python-pptx` in a task environment; check its installed API before relying on advanced features. This small example establishes dimensions and explicit typography. Replace the sample content and choose layouts for the actual task.

```python
from pathlib import Path
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor

output = Path('presentation.pptx')
if output.exists():
    raise FileExistsError(output)
deck = Presentation()
deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
slide = deck.slides.add_slide(deck.slide_layouts[6])
slide.background.fill.solid()
slide.background.fill.fore_color.rgb = RGBColor.from_string('F4F2ED')
box = slide.shapes.add_textbox(Inches(0.8), Inches(0.8), Inches(11.7), Inches(1.2))
p = box.text_frame.paragraphs[0]
run = p.add_run()
run.text = 'The conclusion this slide supports'
run.font.name = 'DejaVu Sans'  # Replace with an available, suitable font.
run.font.size = Pt(34)
run.font.bold = True
run.font.color.rgb = RGBColor.from_string('18363B')
deck.save(output)
```

Add text as runs when mixed formatting matters. Set margins and wrapping intentionally. Use images with known aspect ratios; crop deliberately rather than stretch. For native charts, populate chart data and verify labels, units, legends and embedded values after saving.

Shape coordinates use EMUs; helpers such as `Inches` and `Pt` avoid unit mistakes. Grouped shapes have their own coordinate transform, so child coordinates cannot be compared directly with slide bounds.

Save, reopen, and run the bundled inspector. Then follow `preview.md` in this directory. A successful save does not prove that text fits or that a recipient's application will render identically.

"""Read a drawing set's symbol legend, let a person refine it, and reuse it.

```python
from pinny.legend import read_legend, EngineerLibrary

legend = read_legend("set.pdf", engineer="Hanson & Reyes")   # finds the legend page
for e in legend.needs_review(): ...                          # combined rows, wrapped text, duplicate tags
legend.split("L4"); legend.set_tag("L2", "G"); legend.add((52, 830, 108, 866))
legend.save("legend.json")
```

See ``docs/legend-reader.md``.
"""

from .errors import LegendError
from .library import Difference, EngineerLibrary, LibraryEntry, compare
from .model import CHECK_FLAGS, Legend, LegendEntry, LegendRow, guess_group, nice_name
from .reader import (
    LegendCandidate,
    LegendReader,
    ReaderSettings,
    find_legends,
    read_legend,
    signature_for_box,
    signature_similarity,
)
from .text import TextLine, extract_text_lines

__all__ = [
    "CHECK_FLAGS",
    "Difference",
    "EngineerLibrary",
    "Legend",
    "LegendCandidate",
    "LegendEntry",
    "LegendError",
    "LegendReader",
    "LegendRow",
    "LibraryEntry",
    "ReaderSettings",
    "TextLine",
    "compare",
    "extract_text_lines",
    "find_legends",
    "guess_group",
    "nice_name",
    "read_legend",
    "signature_for_box",
    "signature_similarity",
]

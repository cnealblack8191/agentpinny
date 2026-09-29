"""Scan a drawing set for the symbols in its legend.

```python
from pinny.legend import read_legend
from pinny.scan import scan_set

legend = read_legend("set.pdf")            # then review / edit it
result = scan_set("set.pdf", legend)
result.counts()                            # {"D": 212, "GFI": 31, ...}
result.counts_by_sheet()                   # per page
```

See ``docs/set-scanning.md``.
"""

from .render import CANONICAL_DPI, RENDERER_VERSION, render_page
from .scanner import ScanError, SetScanResult, SetScanSettings, SheetDetection, SheetResult, scan_set

__all__ = [
    "CANONICAL_DPI",
    "RENDERER_VERSION",
    "ScanError",
    "SetScanResult",
    "SetScanSettings",
    "SheetDetection",
    "SheetResult",
    "render_page",
    "scan_set",
]

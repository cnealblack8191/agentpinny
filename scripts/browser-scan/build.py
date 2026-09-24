"""Build index.html: generate the sample sheet and embed it in the page.

    python scripts/browser-scan/build.py
"""
import base64
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory() as tmp:
    pdf = Path(tmp) / "sample.pdf"
    subprocess.run([sys.executable, str(HERE / "make_sample.py"), str(pdf)], check=True, stdout=subprocess.DEVNULL)
    b64 = base64.b64encode(pdf.read_bytes()).decode("ascii")

src = (HERE / "index.src.html").read_text()
(HERE / "index.html").write_text(src.replace("__SAMPLE_B64__", b64))
print(f"Wrote {HERE / 'index.html'}")

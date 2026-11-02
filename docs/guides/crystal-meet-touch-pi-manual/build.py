"""
Render the touch Pi technical manual to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-touch-pi-manual.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-touch-pi-manual.pdf",
           "Technical manual · Crystal Meet touch Pi")

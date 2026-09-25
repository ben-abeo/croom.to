"""
Render the Crystal Meet room setup guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-room-setup.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-room-setup.pdf",
           "How-to guide · Set up a Crystal Meet room")

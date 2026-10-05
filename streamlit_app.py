"""Entry point for Streamlit Community Cloud, which looks for this file at the repository root.

Locally, use `uv run sitt-dashboard` instead. See docs/dashboard.md.
"""

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Community Cloud installs requirements.txt but not this project itself.
sys.path.insert(0, str(ROOT / "src"))
runpy.run_path(str(ROOT / "src" / "sitt" / "dashboard" / "app.py"), run_name="__main__")

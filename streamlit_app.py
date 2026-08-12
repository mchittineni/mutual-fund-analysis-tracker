"""
Entry point for Streamlit Community Cloud.

Community Cloud looks for `streamlit_app.py` at the repository root by default,
so this file exists to make a deployment need zero configuration. The app itself
lives in `dashboard.py`.

**Why `runpy` instead of `import dashboard`:** Streamlit re-executes the entry
script on every interaction, but `import` consults `sys.modules` and returns the
cached module without re-running its body. A shim built on `import` therefore
renders once and then goes blank the first time anyone touches a widget.
`run_path` executes the file fresh on every rerun, which is the behaviour a
Streamlit script needs.

Locally, `streamlit run dashboard.py` and `streamlit run streamlit_app.py` are
equivalent.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Community Cloud runs from the repository root, but not every host does, and
# `from src import ...` inside the dashboard must resolve either way.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

runpy.run_path(str(ROOT / "dashboard.py"), run_name="__main__")

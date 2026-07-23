"""Paste this into Maya's Script Editor (Python tab) and run it.

Re-running it reloads the modules, so you can edit the code and just hit
run again without restarting Maya.
"""

import sys

REPO = r"C:\dev\waldo"

if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Drop cached copies so edits on disk take effect on re-run.
for name in ("waldo_panel", "waldo_manip", "waldo_protocol"):
    sys.modules.pop(name, None)

import waldo_panel

waldo_panel.show()

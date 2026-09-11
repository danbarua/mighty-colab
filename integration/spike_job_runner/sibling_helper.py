"""Sibling of payload.py. Importable ONLY if sys.path[0] is the entry's
own directory -- which `runpy.run_path` does not arrange by itself.
"""

MARKER = "sibling-import-worked"

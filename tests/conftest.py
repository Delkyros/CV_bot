"""Shared pytest setup: make the project root importable for every test module.

Replaces the per-file `sys.path.insert(...)` boilerplate — pytest loads this
automatically before collecting any test.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

"""Import the plugin package under a Python-legal name, isolated from the live home.

``sticky-statusbar`` ships as a **directory plugin** — ``plugin.yaml`` plus
sibling modules, exactly what ``~/.hermes/plugins/sticky-statusbar/`` holds. A
hyphenated directory name is not a legal Python identifier, so the tests import
the same modules under the underscored alias rather than depending on the on-disk
spelling.

The mechanism mirrors how Hermes itself loads a directory plugin
(``hermes_cli/plugin_validate.py``'s capability probe uses the same
``spec_from_file_location`` + ``submodule_search_locations`` pair), so what the
tests exercise is the real import path, not a test-only shim.

``HERMES_HOME`` is redirected to a scratch directory *before* Hermes is imported
anywhere: the anchor store resolves its path through Hermes's own home helper, and
a test run must never write into the profile that is currently live.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

# ── isolation, before any Hermes import ──────────────────────────────────────
# OVERRIDE, not setdefault. The test process usually inherits the live profile's
# HERMES_HOME (the gateway sets it), and the E2E module *installs the plugin into
# $HERMES_HOME and rewrites $HERMES_HOME/config.yaml* — doing that to the live
# profile destroys it. Everything the suite touches must live under this scratch
# directory, whatever the caller's environment said.
_TEST_HOME = tempfile.mkdtemp(prefix="sticky-statusbar-tests-")
os.environ["HERMES_HOME"] = _TEST_HOME

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "sticky-statusbar"
PACKAGE = "sticky_statusbar"

if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: ``__init__.py`` does ``from . import board``, which
    # resolves through this sys.modules entry — the classic package bootstrap
    # ordering. ``__path__`` is what makes the relative siblings findable.
    module.__path__ = [str(PLUGIN_DIR)]
    module.__package__ = PACKAGE
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)

    # A stray flat import (``import board``) would build a SECOND copy of the
    # module with its own globals, silently splitting the state the tests assert
    # on. Pre-seed the aliases so both spellings share one object.
    for name in (
        "anchors",
        "board",
        "publisher",
        "schemas",
        "settings",
        "sinks",
        "status_tool",
        "telemetry",
    ):
        sys.modules.setdefault(name, importlib.import_module(f"{PACKAGE}.{name}"))


# ── shared fixtures ──────────────────────────────────────────────────────────


import pytest  # noqa: E402  (after the isolation block, on purpose)


@pytest.fixture
def plugin():
    """The loaded plugin package."""
    return importlib.import_module(PACKAGE)


@pytest.fixture
def collector(plugin):
    """A fresh collector per test, so counters never leak between them."""
    collector = plugin.get_collector()
    collector.reset()
    yield collector
    collector.reset()


@pytest.fixture
def store(tmp_path):
    """An anchor store writing into the test's own tmp directory."""
    from sticky_statusbar.anchors import AnchorStore

    return AnchorStore(tmp_path / "anchors.json")


@pytest.fixture
def config():
    """Default settings (frozen dataclass — build a new one to change a field)."""
    from sticky_statusbar.settings import StickyConfig

    return StickyConfig()


@pytest.fixture(autouse=True)
def _clean_publishers(plugin):
    """No publisher may survive its test."""
    yield
    plugin.publisher.stop_all()

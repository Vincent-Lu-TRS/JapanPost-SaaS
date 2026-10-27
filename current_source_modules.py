"""Immutable code snapshots for helpers used across code-only Streamlit updates.

Canonical imports and existing workers keep their original globals. Only these
two stateless project helpers are eligible; no caller-supplied path is accepted.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

_SOURCE_ROOT = Path(__file__).resolve().parent
_ALLOWED_MODULES = frozenset({"job_control", "safe_logging"})
_LOAD_LOCK = threading.RLock()
_SNAPSHOTS: dict[tuple[str, str], ModuleType] = {}


def load_current_source_module(name: str) -> ModuleType:
    """Return a snapshot compiled from the exact current project source bytes."""
    if type(name) is not str or name not in _ALLOWED_MODULES:
        raise ValueError("unsupported current-source helper")
    with _LOAD_LOCK:
        root = _SOURCE_ROOT.resolve()
        path = (root / f"{name}.py").resolve()
        if path.parent != root:
            raise ValueError("helper source must remain inside the project")
        source = path.read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        key = (str(path), digest)
        if key in _SNAPSHOTS:
            return _SNAPSHOTS[key]
        identity = hashlib.sha256(str(path).encode("utf-8") + b"\0" + source).hexdigest()
        alias = f"_jppost_source_{name}_{identity}"
        if alias in sys.modules:
            raise ImportError("current-source alias already exists outside the snapshot cache")
        spec = importlib.util.spec_from_file_location(alias, path)
        if spec is None:
            raise ImportError("current-source helper spec is unavailable")
        module = importlib.util.module_from_spec(spec)
        # Register before execution so dataclass/type introspection can resolve
        # the immutable alias. Never replace the canonical module by name.
        sys.modules[alias] = module
        try:
            exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)
        except BaseException:
            if sys.modules.get(alias) is module:
                del sys.modules[alias]
            raise
        _SNAPSHOTS[key] = module
        return module

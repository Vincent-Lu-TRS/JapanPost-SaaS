import importlib
import sys
from types import ModuleType


def import_module_with_retry(module_name: str) -> ModuleType:
    """Retry a module import once when a hot reload loses its sys.modules entry."""
    try:
        return importlib.import_module(module_name)
    except KeyError as exc:
        if exc.args != (module_name,):
            raise
        sys.modules.pop(module_name, None)
        importlib.invalidate_caches()
        return importlib.import_module(module_name)

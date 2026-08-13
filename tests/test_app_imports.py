import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class AppImportTests(unittest.TestCase):
    def test_retries_known_dataclass_reload_error_when_stale_module_is_present(self):
        from app_imports import import_module_with_retry

        module_name = "features.picking_labels"
        stale_module = SimpleNamespace(name="stale")
        loaded_module = SimpleNamespace(name=module_name)
        with patch.dict(sys.modules, {module_name: stale_module}, clear=False):
            with patch(
                "app_imports.importlib.import_module",
                side_effect=[
                    AttributeError("'NoneType' object has no attribute '__dict__'"),
                    loaded_module,
                ],
            ) as import_module:
                result = import_module_with_retry(module_name)

            self.assertNotIn(module_name, sys.modules)

        self.assertIs(result, loaded_module)
        self.assertEqual(import_module.call_count, 2)

    def test_retries_when_importlib_loses_target_module_during_reload(self):
        from app_imports import import_module_with_retry

        loaded_module = SimpleNamespace(name="job_control")
        with patch(
            "app_imports.importlib.import_module",
            side_effect=[KeyError("job_control"), loaded_module],
        ) as import_module:
            result = import_module_with_retry("job_control")

        self.assertIs(result, loaded_module)
        self.assertEqual(import_module.call_count, 2)

    def test_retries_when_dataclass_sees_a_missing_module_namespace_during_reload(self):
        from app_imports import import_module_with_retry

        loaded_module = SimpleNamespace(name="features.picking_labels")
        with patch(
            "app_imports.importlib.import_module",
            side_effect=[
                AttributeError("'NoneType' object has no attribute '__dict__'"),
                loaded_module,
            ],
        ) as import_module:
            result = import_module_with_retry("features.picking_labels")

        self.assertIs(result, loaded_module)
        self.assertEqual(import_module.call_count, 2)


if __name__ == "__main__":
    unittest.main()

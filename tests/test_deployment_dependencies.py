from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentDependencyTests(unittest.TestCase):
    def test_native_dependencies_are_pinned_for_python_312(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertIn("numpy==2.2.6", requirements)
        self.assertIn("pandas==2.2.3", requirements)
        self.assertIn("pyarrow==18.1.0", requirements)
        self.assertIn("reportlab==4.4.3", requirements)

    def test_deployment_guide_requires_python_312_redeployment(self):
        guide = (ROOT / "DEPLOY_GUIDE.md").read_text(encoding="utf-8")

        self.assertIn("Python 3.12", guide)
        self.assertIn("刪除並重新部署", guide)

    def test_streamlit_cloud_apt_manifest_is_disabled_during_upstream_index_outage(self):
        """A stale Cloud apt index must not prevent the Python app from booting."""
        self.assertFalse(
            (ROOT / "packages.txt").exists(),
            "packages.txt re-enables the failing Cloud apt stage; restore it only after the upstream index is fixed",
        )


if __name__ == "__main__":
    unittest.main()

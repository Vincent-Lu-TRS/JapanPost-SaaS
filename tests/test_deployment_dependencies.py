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


if __name__ == "__main__":
    unittest.main()

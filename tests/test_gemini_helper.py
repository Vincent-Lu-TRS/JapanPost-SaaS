import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("streamlit", types.SimpleNamespace(secrets={}, session_state={}))

from bot import gemini_helper


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def generate_content(self, *, model, contents):
        self.prompts.append(contents)
        return _FakeResponse(self.text)


class _FakeClient:
    def __init__(self, models):
        self.models = models


class GeminiHelperTests(unittest.TestCase):
    def setUp(self):
        gemini_helper._HS_CODE_CACHE.clear()

    def test_predict_hs_code_uses_requested_length_and_prompt(self):
        fake_models = _FakeModels("3304990000")
        fake_genai = types.SimpleNamespace(
            Client=lambda api_key: _FakeClient(fake_models)
        )
        fake_google = types.SimpleNamespace(genai=fake_genai)

        with patch.object(gemini_helper, "_get_gemini_key", return_value="key"):
            with patch.dict(sys.modules, {"google": fake_google, "google.genai": fake_genai}):
                code = gemini_helper.predict_hs_code(
                    "Facial Mask",
                    required_length=8,
                    country="FRANCE",
                    country_code="EU",
                )

        self.assertEqual(code, "33049900")
        self.assertIn("8-digit", fake_models.prompts[0])
        self.assertIn("FRANCE", fake_models.prompts[0])

    def test_predict_hs_code_cache_includes_required_length(self):
        fake_models = _FakeModels("3304990000")
        fake_genai = types.SimpleNamespace(
            Client=lambda api_key: _FakeClient(fake_models)
        )
        fake_google = types.SimpleNamespace(genai=fake_genai)

        with patch.object(gemini_helper, "_get_gemini_key", return_value="key"):
            with patch.dict(sys.modules, {"google": fake_google, "google.genai": fake_genai}):
                code6 = gemini_helper.predict_hs_code("Facial Mask", required_length=6)
                code8 = gemini_helper.predict_hs_code("Facial Mask", required_length=8)

        self.assertEqual(code6, "330499")
        self.assertEqual(code8, "33049900")
        self.assertEqual(len(fake_models.prompts), 2)


if __name__ == "__main__":
    unittest.main()

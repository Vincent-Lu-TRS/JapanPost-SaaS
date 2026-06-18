import unittest

from bot.hs_codes import (
    local_hs_code_lookup,
    prepare_hs_codes_for_items,
    required_hs_code_length,
    normalize_hs_code,
)


class HsCodeRuleTests(unittest.TestCase):
    def test_required_length_uses_japan_post_europe_rules(self):
        self.assertEqual(required_hs_code_length("IRELAND", "EU"), 10)
        self.assertEqual(required_hs_code_length("Ireland", "EU"), 10)
        self.assertEqual(required_hs_code_length("IRELAND（アイルランド）", "EU"), 10)
        self.assertEqual(required_hs_code_length("アイルランド", "EU"), 10)
        self.assertEqual(required_hs_code_length("FRANCE", "EU"), 8)
        self.assertEqual(required_hs_code_length("FRANCE（法國）", "EU"), 8)
        self.assertEqual(required_hs_code_length("Guadeloupe", "EU"), 8)
        self.assertEqual(required_hs_code_length("GERMANY", "EU"), 6)
        self.assertEqual(required_hs_code_length("UNITED STATES", "US"), 0)

    def test_normalize_hs_code_accepts_longer_code_by_taking_required_prefix(self):
        self.assertEqual(normalize_hs_code("3304990000", 6), "330499")
        self.assertEqual(normalize_hs_code("3304990000", 8), "33049900")
        self.assertEqual(normalize_hs_code("3304990000", 10), "3304990000")

    def test_normalize_hs_code_rejects_short_code(self):
        self.assertEqual(normalize_hs_code("330499", 8), "")
        self.assertEqual(normalize_hs_code("", 6), "")

    def test_local_hs_code_lookup_covers_common_shipping_items(self):
        self.assertEqual(local_hs_code_lookup("Pillow TRSN9842", 6), "940490")
        self.assertEqual(local_hs_code_lookup("Facial Mask(No Alcohol) TRSN6764", 6), "330499")
        self.assertEqual(local_hs_code_lookup("Hair Conditioner", 6), "330590")

    def test_prepare_hs_codes_for_items_dedupes_and_uses_required_length(self):
        calls = []

        def fake_predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append((item_name, required_length, country, country_code))
            return "3304990000"

        items = [
            {"index": "1", "pkg": "Facial Mask"},
            {"index": "2", "pkg": "Facial Mask"},
        ]

        codes = prepare_hs_codes_for_items(
            items,
            country_raw="IRELAND",
            country_code="EU",
            predictor=fake_predictor,
        )

        self.assertEqual(codes, {"1": "3304990000", "2": "3304990000"})
        self.assertEqual(calls, [("Facial Mask", 10, "IRELAND", "EU")])

    def test_prepare_hs_codes_for_items_skips_non_required_country(self):
        calls = []

        def fake_predictor(*args, **kwargs):
            calls.append((args, kwargs))
            return "330499"

        codes = prepare_hs_codes_for_items(
            [{"index": "1", "pkg": "Facial Mask"}],
            country_raw="UNITED STATES",
            country_code="US",
            predictor=fake_predictor,
        )

        self.assertEqual(codes, {})
        self.assertEqual(calls, [])

    def test_prepare_hs_codes_for_items_uses_local_fallback_when_predictor_fails(self):
        calls = []

        def exhausted_predictor(item_name, *, required_length=6, country="", country_code="", log_cb=None):
            calls.append(item_name)
            return ""

        codes = prepare_hs_codes_for_items(
            [{"index": "1", "pkg": "Pillow TRSN9842"}],
            country_raw="GERMANY",
            country_code="EU",
            predictor=exhausted_predictor,
        )

        self.assertEqual(codes, {"1": "940490"})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

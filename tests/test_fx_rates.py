import unittest

from fx_rates import USD_JPY_ENDPOINTS, parse_usd_jpy_rate_response


class FxRatesTests(unittest.TestCase):
    def test_parse_frankfurter_v2_rates_response(self):
        rate, rate_date = parse_usd_jpy_rate_response(
            [{"date": "2026-06-19", "base": "USD", "quote": "JPY", "rate": 161.08}]
        )

        self.assertEqual(rate, 161.08)
        self.assertEqual(rate_date, "2026-06-19")

    def test_parse_frankfurter_v1_latest_response(self):
        rate, rate_date = parse_usd_jpy_rate_response(
            {"date": "2026-06-18", "base": "USD", "rates": {"JPY": 160.93}}
        )

        self.assertEqual(rate, 160.93)
        self.assertEqual(rate_date, "2026-06-18")

    def test_parse_invalid_response_returns_none(self):
        self.assertEqual(parse_usd_jpy_rate_response({"rates": {}}), (None, ""))

    def test_fallback_provider_is_available_when_frankfurter_is_unreachable(self):
        self.assertIn("https://api.exchangerate-api.com/v4/latest/USD", USD_JPY_ENDPOINTS)
        rate, rate_date = parse_usd_jpy_rate_response(
            {"date": "2026-08-06", "base": "USD", "rates": {"JPY": 157.61}}
        )

        self.assertEqual(rate, 157.61)
        self.assertEqual(rate_date, "2026-08-06")


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timedelta, timezone

from local_time import JST, format_jst


class LocalTimeTests(unittest.TestCase):
    def test_utc_value_is_rendered_as_japan_time_without_timezone_suffix(self):
        value = datetime(2026, 8, 14, 3, 10, tzinfo=timezone.utc)

        self.assertEqual(format_jst(value, "%Y-%m-%d %H:%M"), "2026-08-14 12:10")
        self.assertNotIn("GMT+9", format_jst(value, "%Y-%m-%d %H:%M"))

    def test_naive_value_is_treated_as_japan_local_time(self):
        value = datetime(2026, 8, 14, 12, 10)

        self.assertEqual(format_jst(value, "%H:%M"), "12:10")
        self.assertEqual(JST.utcoffset(value), timedelta(hours=9))


if __name__ == "__main__":
    unittest.main()

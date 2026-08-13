import unittest

from safe_logging import redact_operational_log, safe_log_event


class SafeLoggingTests(unittest.TestCase):
    def test_safe_log_event_allows_only_aggregate_fields(self):
        logs = []

        safe_log_event(
            logs.append,
            "pending_read_failed",
            count=2,
            seconds=1.25,
            error_type="PermissionError",
            first_row=2,
            last_row=3,
        )

        self.assertEqual(
            logs,
            [
                "pending_read_failed count=2 seconds=1.25 "
                "error_type=PermissionError first_row=2 last_row=3"
            ],
        )

    def test_safe_log_event_rejects_identifier_reason_and_unknown_event(self):
        logs = []

        with self.assertRaises(ValueError):
            safe_log_event(logs.append, "pending_read_failed", order_id="ORDER-8490")
        with self.assertRaises(ValueError):
            safe_log_event(logs.append, "preflight_blocked", reason="ORDER-8490")
        with self.assertRaises(ValueError):
            safe_log_event(logs.append, "ORDER-8490", count=1)

        self.assertEqual(logs, [])

    def test_safe_log_event_never_serializes_raw_exception(self):
        logs = []
        secret_exception = RuntimeError("receiver@example.com ORDER-8490")

        with self.assertRaises(ValueError):
            safe_log_event(
                logs.append,
                "job_exception",
                error_type=secret_exception,
            )

        self.assertEqual(logs, [])

    def test_redact_operational_log_removes_order_receiver_tracking_and_pii(self):
        message = (
            "ORDER=WhoWhy-Test6\nReceiver=David_Derrick "
            "tracking=LX324329616JP email=receiver@example.com ref=123456789"
        )

        redacted = redact_operational_log(
            message,
            sensitive_values=("WhoWhy-Test6", "David_Derrick"),
        )

        self.assertNotIn("\n", redacted)
        self.assertNotIn("WhoWhy-Test6", redacted)
        self.assertNotIn("David_Derrick", redacted)
        self.assertNotIn("LX324329616JP", redacted)
        self.assertNotIn("receiver@example.com", redacted)
        self.assertNotIn("123456789", redacted)
        self.assertGreaterEqual(redacted.count("[REDACTED]"), 5)


if __name__ == "__main__":
    unittest.main()

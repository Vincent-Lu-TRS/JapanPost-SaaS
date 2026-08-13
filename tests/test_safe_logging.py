import unittest
from pathlib import Path

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

    def test_redacted_message_stays_safe_through_session_and_ui_log_paths(self):
        sensitive_values = ("ORDER-SECRET", "Secret Recipient")
        raw_message = (
            "order=ORDER-SECRET receiver=Secret Recipient "
            "tracking=EE123456789JP email=receiver@example.com"
        )

        session_logs = [
            redact_operational_log(raw_message, sensitive_values=sensitive_values)
        ]
        rendered_log = "\n".join(
            redact_operational_log(line, sensitive_values=sensitive_values)
            for line in session_logs
        )

        for secret in (
            "ORDER-SECRET",
            "Secret Recipient",
            "EE123456789JP",
            "receiver@example.com",
        ):
            self.assertNotIn(secret, session_logs[0])
            self.assertNotIn(secret, rendered_log)

    def test_automation_and_sheets_redact_at_source_boundaries(self):
        root = Path(__file__).parents[1]
        automation_source = root.joinpath("bot", "automation.py").read_text(encoding="utf-8")
        sheets_source = root.joinpath("bot", "sheets.py").read_text(encoding="utf-8")

        self.assertIn("redact_operational_log", automation_source)
        self.assertIn("safe_message = redact_operational_log", automation_source)
        self.assertNotIn("logging.info(msg)", automation_source)
        self.assertNotIn("format_exc()", automation_source)
        run_body = automation_source.split("def run_automation(", 1)[1]
        self.assertNotIn("log_cb=log_cb", run_body)
        self.assertIn("safe_message = redact_operational_log", sheets_source)
        self.assertNotIn('error = "回填後讀回驗證失敗：" + ", ".join(missing[:8])', sheets_source)
        self.assertNotIn('"error": str(e)', sheets_source)


if __name__ == "__main__":
    unittest.main()

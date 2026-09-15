import unittest
from pathlib import Path

from safe_logging import (
    build_safe_automation_logger,
    redact_operational_log,
    safe_log_event,
    log_runtime_failure,
    log_runtime_environment,
)


class SafeLoggingTests(unittest.TestCase):
    def test_browser_runtime_environment_log_contains_only_allowlisted_fingerprint(self):
        logs = []
        log_runtime_environment(
            logs.append,
            {
                "system": "linux",
                "os_id": "debian",
                "os_version": "12",
                "machine": "x86_64",
                "python_major": 3,
                "python_minor": 12,
                "libc_name": "glibc",
                "libc_version": "2.36",
            },
        )
        self.assertEqual(
            logs,
            [
                "browser_runtime_profile system=linux os_id=debian os_version=12 "
                "machine=x86_64 python_minor=3.12 libc_name=glibc libc_version=2.36"
            ],
        )

    def test_browser_runtime_environment_log_rejects_injected_or_unallowlisted_values(self):
        fields = {
            "system": "linux", "os_id": "debian", "os_version": "12",
            "machine": "x86_64", "python_major": 3, "python_minor": 12,
            "libc_name": "glibc", "libc_version": "2.36",
        }
        logs = []
        bad = dict(fields)
        bad["os_version"] = "12\norder=private"
        with self.assertRaises(ValueError):
            log_runtime_environment(logs.append, bad)
        bad = dict(fields)
        bad["HOME"] = "/private/path"
        with self.assertRaises(ValueError):
            log_runtime_environment(logs.append, bad)
        self.assertEqual(logs, [])

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

    def test_browser_runtime_log_records_only_safe_classification(self):
        logs = []

        safe_log_event(
            logs.append,
            "browser_runtime_failed",
            stage="browser_install",
            code="browser_download",
            retryable=True,
            attempts=2,
            http_status=503,
        )

        self.assertEqual(
            logs,
            [
                "browser_runtime_failed stage=browser_install code=browser_download "
                "retryable=true attempts=2 http_status=503"
            ],
        )

    def test_browser_runtime_log_rejects_raw_urls_and_order_data(self):
        logs = []
        with self.assertRaises(ValueError):
            safe_log_event(
                logs.append,
                "browser_runtime_failed",
                stage="browser_install",
                code="https://example.invalid?token=secret",
                retryable=True,
            )
        with self.assertRaises(ValueError):
            safe_log_event(
                logs.append,
                "browser_runtime_failed",
                stage="browser_install",
                code="browser_download",
                retryable=True,
                order_id="im2041000",
            )
        self.assertEqual(logs, [])

    def test_runtime_failure_helper_discards_unsafe_exception_details(self):
        logs = []
        error = RuntimeError("receiver@example.com im2041000 https://secret.invalid")
        error.stage = "browser_install"
        error.code = "browser_download"
        error.retryable = True
        error.attempts = 2
        error.http_status = 503

        log_runtime_failure(logs.append, error)

        self.assertEqual(
            logs,
            [
                "browser_runtime_failed stage=browser_install code=browser_download "
                "retryable=true attempts=2 http_status=503"
            ],
        )

    def test_runtime_failure_helper_preserves_runtime_fence_safety_codes(self):
        for code in (
            "bootstrap_protocol_error",
            "subreaper_unavailable",
            "operation_children_leaked",
            "operation_cleanup_failed",
        ):
            with self.subTest(code=code):
                logs = []
                error = RuntimeError("private diagnostic details must not be logged")
                error.stage = "bootstrap"
                error.code = code
                error.retryable = True

                log_runtime_failure(logs.append, error)

                self.assertEqual(
                    logs,
                    [
                        "browser_runtime_failed stage=bootstrap "
                        f"code={code} retryable=true attempts=0"
                    ],
                )

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

    def test_safe_automation_logger_redacts_before_callback_and_python_logging(self):
        callback_messages = []

        class RecordingLogger:
            def __init__(self):
                self.messages = []

            def info(self, template, message):
                self.messages.append(template % message)

        logger = RecordingLogger()
        emit = build_safe_automation_logger(
            callback_messages.append,
            sensitive_values=("SERVER-SECRET", "Synthetic Receiver"),
            logger=logger,
        )

        emit(
            "response body SERVER-SECRET receiver=Synthetic Receiver "
            "tracking=LX123456789JP email=private@example.com"
        )

        self.assertEqual(callback_messages, logger.messages)
        for output in callback_messages + logger.messages:
            self.assertNotIn("SERVER-SECRET", output)
            self.assertNotIn("Synthetic Receiver", output)
            self.assertNotIn("LX123456789JP", output)
            self.assertNotIn("private@example.com", output)

    def test_automation_source_never_logs_raw_response_bodies_or_exception_text(self):
        automation_source = Path(__file__).parents[1].joinpath("bot", "automation.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("build_safe_automation_logger", automation_source)
        self.assertNotIn("body[:", automation_source)
        self.assertNotIn("body_snip", automation_source)
        self.assertNotRegex(automation_source, r'_log\(f[^\n]*(?:\{e\}|\{exc\}|\{_re_err\})')
        self.assertNotIn("logging.info", automation_source)

    def test_automation_and_sheets_redact_at_source_boundaries(self):
        root = Path(__file__).parents[1]
        automation_source = root.joinpath("bot", "automation.py").read_text(encoding="utf-8")
        sheets_source = root.joinpath("bot", "sheets.py").read_text(encoding="utf-8")

        self.assertIn("build_safe_automation_logger", automation_source)
        self.assertIn("_log = build_safe_automation_logger", automation_source)
        self.assertNotIn("logging.info(msg)", automation_source)
        self.assertNotIn("format_exc()", automation_source)
        run_body = automation_source.split("def run_automation(", 1)[1]
        self.assertNotIn("log_cb=log_cb", run_body)
        self.assertIn("safe_message = redact_operational_log", sheets_source)
        self.assertNotIn('error = "回填後讀回驗證失敗：" + ", ".join(missing[:8])', sheets_source)
        self.assertNotIn('"error": str(e)', sheets_source)


if __name__ == "__main__":
    unittest.main()

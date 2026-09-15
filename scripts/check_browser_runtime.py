#!/usr/bin/env python3
"""Verify the pinned Linux Chromium runtime and emit a safe JSON result."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.browser_runtime import (
    BrowserRuntimeBootstrap,
    RuntimeSetupError,
    _manifest_digest,
    _playwright_browser_metadata,
    _release_digest,
    load_runtime_profile,
)
from bot.playwright_runtime import _load_verified_assets


_SAFE_FIELDS = (
    "release_digest",
    "asset_digest",
    "playwright_version",
    "browser_revision",
    "platform",
    "stage",
    "status",
    "elapsed_ms",
)
_STAGES = {
    "metadata",
    "asset_verification",
    "browser_bootstrap",
    "blank_page_launch",
    "complete",
}
_RETRYABLE_LAUNCH_MARKERS = (
    "targetclosederror",
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser process closed",
)


def _safe_platform() -> str:
    value = f"{platform.system().lower()}-{platform.machine().lower()}"
    sanitized = re.sub(r"[^a-z0-9._-]", "_", value)[:64]
    return sanitized or "unknown"


def _safe_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-fA-F]{64}", value):
        return value.lower()
    if value in {"missing", "unavailable"}:
        return value
    return "unavailable"


def _safe_metadata(value: str, *, pattern: str) -> str:
    if len(value) <= 64 and re.fullmatch(pattern, value):
        return value
    return "unavailable"


def _close_quietly(resource: Any) -> None:
    if resource is not None:
        try:
            resource.close()
        except Exception:
            pass


def _launch_blank_page(handle, profile: dict[str, object], browser_version: str) -> None:
    """Launch the verified executable headlessly and navigate to about:blank."""

    from playwright.sync_api import sync_playwright

    launch_args = list(profile["launch_args"])
    fallback_args = list(dict.fromkeys(launch_args + list(profile["fallback_args"])))
    timeout_ms = int(profile["probe_timeout_seconds"]) * 1000

    with sync_playwright() as playwright:
        for attempt, args in enumerate((launch_args, fallback_args)):
            browser = context = page = None
            try:
                browser = playwright.chromium.launch(
                    headless=True,
                    args=args,
                    env=dict(handle.env),
                    executable_path=str(handle.executable_path),
                )
                if browser.version != browser_version:
                    raise RuntimeError("pinned browser version mismatch")
                context = browser.new_context()
                page = context.new_page()
                page.goto("about:blank", wait_until="load", timeout=timeout_ms)
                if page.url != "about:blank" or page.title() != "":
                    raise RuntimeError("blank page health check failed")
                return
            except Exception as exc:
                retryable = any(marker in str(exc).lower() for marker in _RETRYABLE_LAUNCH_MARKERS)
                if attempt != 0 or not retryable:
                    raise
            finally:
                _close_quietly(page)
                _close_quietly(context)
                _close_quietly(browser)


def check_runtime(
    bootstrap: BrowserRuntimeBootstrap | None = None,
) -> tuple[dict[str, object], int]:
    """Return the allowlisted status payload and CLI exit code."""

    started = time.monotonic()
    bootstrap = bootstrap or BrowserRuntimeBootstrap()
    payload: dict[str, object] = {
        "release_digest": "unavailable",
        "asset_digest": "unavailable",
        "playwright_version": "unavailable",
        "browser_revision": "unavailable",
        "platform": _safe_platform(),
        "stage": "metadata",
        "status": "error",
        "elapsed_ms": 0,
    }
    stage = "metadata"

    try:
        payload["release_digest"] = _safe_digest(_release_digest(bootstrap.project_root))
        payload["asset_digest"] = _safe_digest(_manifest_digest(bootstrap.asset_root))
        profile = load_runtime_profile(bootstrap.profile_path)

        stage = "asset_verification"
        payload["stage"] = stage
        asset_digest, _assets = _load_verified_assets(
            bootstrap.asset_root,
            deadline=time.monotonic() + float(profile["prepare_timeout_seconds"]),
        )
        payload["asset_digest"] = _safe_digest(asset_digest)

        stage = "metadata"
        payload["stage"] = stage
        revision, browser_version = _playwright_browser_metadata()
        playwright_version = importlib.metadata.version("playwright")
        payload["browser_revision"] = _safe_metadata(revision, pattern=r"[0-9]{1,16}")
        payload["playwright_version"] = _safe_metadata(
            playwright_version,
            pattern=r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+._A-Za-z0-9]*)?",
        )
        if "unavailable" in (payload["browser_revision"], payload["playwright_version"]):
            raise RuntimeSetupError("browser_profile", "profile_unsupported")

        stage = "browser_bootstrap"
        payload["stage"] = stage
        handle = bootstrap.create_manager().ensure_ready(bootstrap.profile_key())
        if (
            handle.browser_flavor != "chromium"
            or handle.browser_revision != revision
            or handle.browser_version != browser_version
        ):
            raise RuntimeSetupError("browser_profile", "profile_unsupported")

        stage = "blank_page_launch"
        payload["stage"] = stage
        _launch_blank_page(handle, profile, browser_version)

        payload["stage"] = "complete"
        payload["status"] = "ready"
        return payload, 0
    except Exception:
        # Runtime exceptions can contain paths, URLs, or process output. Keep
        # the diagnostic useful without reflecting any of that data.
        payload["stage"] = stage if stage in _STAGES else "metadata"
        payload["status"] = "error"
        return payload, 1
    finally:
        payload["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))


def main(*, bootstrap: BrowserRuntimeBootstrap | None = None) -> int:
    payload, exit_code = check_runtime(bootstrap)
    safe_payload = {field: payload[field] for field in _SAFE_FIELDS}
    sys.stdout.write(json.dumps(safe_payload, sort_keys=True, separators=(",", ":")) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

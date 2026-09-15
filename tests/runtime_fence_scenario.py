"""Linux-only process fixture for the runtime-fence integration tests."""

import json
import io
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bot.browser_bootstrap import RuntimeHandle
from bot.runtime_fence import execute_request


def _spawn_setsid_grandchild(marker: Path) -> None:
    child_script = (
        "import os,time; os.setsid(); child=os.fork(); "
        "os._exit(0) if child else None; os.setsid(); "
        f"open({json.dumps(str(marker))}, 'w', encoding='ascii').write(str(os.getpid())); "
        "time.sleep(120)"
    )
    process = subprocess.Popen([sys.executable, "-c", child_script], close_fds=True)
    process.wait(timeout=3)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not marker.exists():
        raise RuntimeError("grandchild failed to signal readiness")


def _stuck_in_deb_extract(marker: Path, deadline: float) -> None:
    from unittest.mock import patch

    import bot.playwright_runtime as playwright_runtime

    archive_stream = io.BytesIO()
    with tarfile.open(fileobj=archive_stream, mode="w") as archive:
        member = tarfile.TarInfo("usr/lib/x86_64-linux-gnu/libfixture.so")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    archive_bytes = archive_stream.getvalue()
    package = marker.parent / "fixture.deb"
    package.write_bytes(b"synthetic deb container")

    def blocked_extract(_archive, _member, **_kwargs):
        time.sleep(120)

    with patch.object(playwright_runtime, "_check_deadline", lambda *_args: None), patch.object(
        playwright_runtime, "_ar_data_member_bytes", return_value=archive_bytes
    ), patch.object(tarfile.TarFile, "extract", blocked_extract):
        playwright_runtime._extract_deb(package, marker.parent / "runtime-libs", deadline=deadline)


def _install_late_descendant_race(marker: Path) -> None:
    """Spawn a new detached child exactly after cleanup's first SIGKILL scan."""

    from bot import browser_runtime

    original_signal = browser_runtime._signal_linux_descendants
    state = {"spawned": False}

    def signal_with_late_child(descendants, signal_number):
        if signal_number == signal.SIGKILL and not state["spawned"]:
            state["spawned"] = True
            script = (
                "import os,time; os.setsid(); "
                f"open({json.dumps(str(marker))}, 'w', encoding='ascii').write(str(os.getpid())); "
                "time.sleep(120)"
            )
            subprocess.Popen([sys.executable, "-c", script], start_new_session=True, close_fds=True)
            ready_deadline = time.monotonic() + 1.0
            while not marker.exists() and time.monotonic() < ready_deadline:
                time.sleep(0.005)
            if not marker.exists():
                raise RuntimeError("late descendant did not signal readiness")
        original_signal(descendants, signal_number)

    browser_runtime._signal_linux_descendants = signal_with_late_child
    return original_signal


def main() -> int:
    scenario = sys.argv[1]
    marker = Path(sys.argv[2])

    class ScenarioBootstrap:
        def prepare_in_process(self, key: str, deadline: float) -> RuntimeHandle:
            if scenario == "late-descendant":
                term_resistant_marker = marker.with_name(f"{marker.name}.term-resistant")
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"open({json.dumps(str(term_resistant_marker))}, 'w', encoding='ascii').write(str(os.getpid())); "
                        "time.sleep(120)",
                    ],
                    close_fds=True,
                )
                _install_late_descendant_race(marker)
            else:
                _spawn_setsid_grandchild(marker)
            if scenario == "crash":
                raise RuntimeError("secret test traceback marker")
            if scenario == "timeout":
                _stuck_in_deb_extract(marker, deadline)
            return RuntimeHandle(key, Path("/tmp/ms-playwright/chrome"), "r1", {})

        def validate_in_process(self, _handle: object, _deadline: float) -> bool:
            return False

    request = {
        "protocol": 1,
        "operation": "prepare",
        "deadline": time.monotonic() + 120,
        "key": "integration-profile",
    }
    response = execute_request(request, bootstrap_factory=lambda **_kwargs: ScenarioBootstrap())
    print(json.dumps(response, separators=(",", ":")))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

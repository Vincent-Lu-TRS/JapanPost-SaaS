"""User-space Linux runtime libraries required by Playwright Chromium.

Streamlit Community Cloud can temporarily fail before the app starts when its
``packages.txt`` apt index is stale.  Chromium still needs a few shared
libraries, so this module downloads the small, hash-pinned Debian packages and
extracts only their library files into ``/tmp``.  No root permission or apt
operation is required.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import platform
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.request import Request, urlopen


RUNTIME_VERSION = "2026-09-09-t1"
DEFAULT_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "jppost-playwright-runtime"
MAX_PACKAGE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class RuntimePackage:
    name: str
    filename: str
    url: str
    sha256: str


# Debian Trixie packages are used because the current Streamlit image uses the
# Trixie-compatible ABI (the old packages.txt also used the *-0t64 names).
# The URLs and SHA-256 values are pinned so an unexpected mirror response is
# never extracted into the application process.
REQUIRED_RUNTIME_PACKAGES = (
    RuntimePackage(
        name="libnspr4",
        filename="libnspr4_4.40-1_amd64.deb",
        url="https://deb.debian.org/debian/pool/main/n/nspr/libnspr4_4.40-1_amd64.deb",
        sha256="824213bad27fe97306e626ea6feb064f5c009260d3d35ab311431466673978dd",
    ),
    RuntimePackage(
        name="libnss3",
        filename="libnss3_3.128-1_amd64.deb",
        url="https://deb.debian.org/debian/pool/main/n/nss/libnss3_3.128-1_amd64.deb",
        sha256="826b69cfeeb77bd32dee0f7515729c45b745ec9c3e5f6b04614ba714b9c78b62",
    ),
    RuntimePackage(
        name="libasound2t64",
        filename="libasound2t64_1.2.16.1-1_amd64.deb",
        url="https://deb.debian.org/debian/pool/main/a/alsa-lib/libasound2t64_1.2.16.1-1_amd64.deb",
        sha256="8e6970b2bdf54b1f66a75b78018f9640cda78dca440a15293fdafbcf697cc95e",
    ),
)

REQUIRED_RUNTIME_LIBRARY_NAMES = (
    "libnspr4.so",
    "libnss3.so",
    "libasound.so.2",
)


@dataclass(frozen=True)
class PlaywrightRuntimeResult:
    ok: bool
    env: dict[str, str]
    lib_dir: Path
    message: str = ""
    downloaded: bool = False
    reused: bool = False


_RUNTIME_LOCK = threading.Lock()


def _library_available(soname: str) -> bool:
    try:
        ctypes.CDLL(soname)
    except OSError:
        return False
    return True


def _library_dir(runtime_root: Path) -> Path:
    return runtime_root / "usr" / "lib" / "x86_64-linux-gnu"


def _vendor_libraries_ready(runtime_root: Path) -> bool:
    marker = runtime_root / ".ready"
    try:
        marker_matches = marker.read_text(encoding="utf-8").strip() == RUNTIME_VERSION
    except (OSError, UnicodeError):
        marker_matches = False
    return marker_matches and _vendor_library_files_present(runtime_root)


def _vendor_library_files_present(runtime_root: Path) -> bool:
    library_dir = _library_dir(runtime_root)
    return all((library_dir / name).is_file() for name in REQUIRED_RUNTIME_LIBRARY_NAMES)


def _prepend_library_path(lib_dir: Path, current: str) -> str:
    prefix = str(lib_dir)
    return prefix if not current else f"{prefix}{os.pathsep}{current}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_package(package: RuntimePackage, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    partial.unlink(missing_ok=True)
    request = Request(
        package.url,
        headers={"User-Agent": "jppost-playwright-runtime/1"},
    )
    total = 0
    digest = hashlib.sha256()
    try:
        with urlopen(request, timeout=120) as response, partial.open("wb") as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PACKAGE_BYTES:
                    raise RuntimeError(f"{package.name} package exceeds size limit")
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest().lower() != package.sha256.lower():
            raise RuntimeError(f"{package.name} package checksum mismatch")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _ar_data_member(package_path: Path) -> bytes:
    raw = package_path.read_bytes()
    if not raw.startswith(b"!<arch>\n"):
        raise RuntimeError("invalid Debian package archive")
    offset = len(b"!<arch>\n")
    while offset + 60 <= len(raw):
        header = raw[offset : offset + 60]
        name = header[:16].decode("utf-8", errors="replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii", errors="strict").strip())
        except ValueError as exc:
            raise RuntimeError("invalid Debian package member header") from exc
        start = offset + 60
        end = start + size
        if end > len(raw):
            raise RuntimeError("truncated Debian package archive")
        if name.startswith("data.tar."):
            return raw[start:end]
        offset = end + (size % 2)
    raise RuntimeError("Debian package has no data archive")


def _safe_tar_member_name(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise RuntimeError("unsafe path in Debian package")
    return "/".join(parts)


def _extract_deb(package_path: Path, destination: Path) -> None:
    """Extract only x86_64 shared libraries from a .deb without root access."""

    destination.mkdir(parents=True, exist_ok=True)
    data_archive = _ar_data_member(package_path)
    destination_root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data_archive), mode="r:*") as archive:
        for member in archive.getmembers():
            normalized = _safe_tar_member_name(member.name)
            if not normalized.startswith("usr/lib/x86_64-linux-gnu/"):
                continue
            target = (destination / normalized).resolve()
            if not target.is_relative_to(destination_root):
                raise RuntimeError("unsafe library path in Debian package")
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                if not link_target.is_relative_to(destination_root):
                    raise RuntimeError("unsafe library link in Debian package")
            member.name = normalized
            archive.extract(member, path=destination)


def _runtime_failure(
    env: dict[str, str],
    lib_dir: Path,
    message: str,
) -> PlaywrightRuntimeResult:
    return PlaywrightRuntimeResult(
        ok=False,
        env=env,
        lib_dir=lib_dir,
        message=message,
    )


def prepare_playwright_runtime(
    *,
    env: Mapping[str, str] | None = None,
    runtime_root: Path | str | None = None,
    platform_name: str | None = None,
    machine_name: str | None = None,
    library_checker: Callable[[str], bool] | None = None,
    downloader: Callable[[RuntimePackage, Path], None] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    apply_to_process_env: bool = True,
) -> PlaywrightRuntimeResult:
    """Prepare Chromium's Linux libraries and return the child-process env."""

    child_env = dict(os.environ if env is None else env)
    system = (platform_name or platform.system()).lower()
    machine = (machine_name or platform.machine()).lower()
    root = Path(runtime_root) if runtime_root is not None else DEFAULT_RUNTIME_ROOT
    lib_dir = _library_dir(root)

    if system != "linux":
        return PlaywrightRuntimeResult(True, child_env, lib_dir, "Linux libraries not required")
    if machine not in {"x86_64", "amd64"}:
        return _runtime_failure(child_env, lib_dir, f"unsupported Linux architecture: {machine}")

    checker = library_checker or _library_available
    if all(checker(name) for name in REQUIRED_RUNTIME_LIBRARY_NAMES):
        return PlaywrightRuntimeResult(True, child_env, lib_dir, "system libraries available")

    active_downloader = downloader or _download_package
    active_extractor = extractor or _extract_deb
    was_downloaded = False
    was_reused = False

    with _RUNTIME_LOCK:
        if _vendor_libraries_ready(root):
            was_reused = True
        else:
            try:
                root.mkdir(parents=True, exist_ok=True)
                for package in REQUIRED_RUNTIME_PACKAGES:
                    package_path = root / package.filename
                    if downloader is None and (
                        not package_path.is_file()
                        or _file_sha256(package_path).lower() != package.sha256.lower()
                    ):
                        _download_package(package, package_path)
                        was_downloaded = True
                    elif downloader is not None:
                        active_downloader(package, package_path)
                        was_downloaded = True
                    active_extractor(package_path, root)
                if not _vendor_library_files_present(root):
                    return _runtime_failure(
                        child_env,
                        lib_dir,
                        "runtime libraries were not extracted completely",
                    )
                (root / ".ready").write_text(RUNTIME_VERSION, encoding="utf-8")
            except Exception as exc:
                return _runtime_failure(
                    child_env,
                    lib_dir,
                    f"runtime library preparation failed: {type(exc).__name__}",
                )

    child_env["LD_LIBRARY_PATH"] = _prepend_library_path(
        lib_dir,
        child_env.get("LD_LIBRARY_PATH", ""),
    )
    if apply_to_process_env:
        os.environ["LD_LIBRARY_PATH"] = child_env["LD_LIBRARY_PATH"]
    return PlaywrightRuntimeResult(
        ok=True,
        env=child_env,
        lib_dir=lib_dir,
        message="vendored runtime libraries ready",
        downloaded=was_downloaded,
        reused=was_reused,
    )

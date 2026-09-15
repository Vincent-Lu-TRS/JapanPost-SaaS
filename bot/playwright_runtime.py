"""Verified user-space Linux libraries required by Playwright Chromium.

Production startup reads the hash-pinned Debian packages shipped in the
repository. It never fetches Debian URLs at runtime, and publishes extracted
libraries only after the complete set passes checks.
"""

from __future__ import annotations

import ctypes
import contextlib
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


DEFAULT_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "jppost-playwright-runtime"
DEFAULT_ASSET_ROOT = (
    Path(__file__).resolve().parent.parent
    / "vendor"
    / "playwright-runtime-v1-linux-x86_64"
)
EXPECTED_RUNTIME_MANIFEST_SHA256 = "1d7dd656cfbe09d6f33424b3c1413a6a33b52b52fdb3224ffd60d0f8b2f3fdce"
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class RuntimePackage:
    name: str
    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class PlaywrightRuntimeResult:
    ok: bool
    env: dict[str, str]
    lib_dir: Path
    message: str = ""
    downloaded: bool = False
    reused: bool = False
    error_code: str = ""
    manifest_digest: str = ""


_RUNTIME_LOCK = threading.Lock()


def _library_available(soname: str) -> bool:
    try:
        ctypes.CDLL(soname)
    except OSError:
        return False
    return True


def _library_dir(runtime_root: Path) -> Path:
    return runtime_root / "usr" / "lib" / "x86_64-linux-gnu"


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("runtime preparation deadline exceeded")


def _vendor_libraries_ready(
    runtime_root: Path, *, manifest_digest: str, platform_fingerprint: str,
    deadline: float | None = None,
) -> bool:
    _check_deadline(deadline)
    marker = runtime_root / ".ready.json"
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(metadata, dict):
        return False
    if (
        metadata.get("format") != 1
        or metadata.get("manifest_digest") != manifest_digest
        or metadata.get("platform") != platform_fingerprint
        or not isinstance(metadata.get("libraries"), dict)
    ):
        return False
    library_dir = _library_dir(runtime_root)
    hashes = metadata["libraries"]
    if set(hashes) != set(REQUIRED_RUNTIME_LIBRARY_NAMES):
        return False
    try:
        for name in REQUIRED_RUNTIME_LIBRARY_NAMES:
            _check_deadline(deadline)
            if not (library_dir / name).is_file():
                return False
            if _file_sha256(library_dir / name, deadline=deadline) != hashes[name]:
                return False
        return True
    except TimeoutError:
        raise
    except OSError:
        return False


def _vendor_library_files_present(runtime_root: Path) -> bool:
    library_dir = _library_dir(runtime_root)
    return all((library_dir / name).is_file() for name in REQUIRED_RUNTIME_LIBRARY_NAMES)


def _prepend_library_path(lib_dir: Path, current: str) -> str:
    prefix = str(lib_dir)
    return prefix if not current else f"{prefix}{os.pathsep}{current}"


def _file_sha256(path: Path, *, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check_deadline(deadline)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    _check_deadline(deadline)
    return digest.hexdigest()


class RuntimeAssetError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _load_verified_assets(
    asset_root: Path,
    *,
    deadline: float | None = None,
) -> tuple[str, tuple[tuple[RuntimePackage, Path], ...]]:
    _check_deadline(deadline)
    manifest_path = asset_root / "manifest.json"
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RuntimeAssetError("asset_manifest_invalid")
        manifest_bytes = manifest_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeAssetError("asset_missing") from exc
    except OSError as exc:
        raise RuntimeAssetError("asset_missing") from exc
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeAssetError("asset_manifest_invalid") from exc
    if (
        not isinstance(manifest, dict)
        or (
            asset_root.resolve() == DEFAULT_ASSET_ROOT.resolve()
            and hashlib.sha256(manifest_bytes).hexdigest() != EXPECTED_RUNTIME_MANIFEST_SHA256
        )
        or not REQUIRED_RUNTIME_PACKAGES
        or manifest.get("format") != 1
        or manifest.get("platform") != "linux-x86_64"
        or not isinstance(manifest.get("packages"), list)
        or len(manifest["packages"]) != len(REQUIRED_RUNTIME_PACKAGES)
    ):
        raise RuntimeAssetError("asset_manifest_invalid")

    verified: list[tuple[RuntimePackage, Path]] = []
    for package, item in zip(REQUIRED_RUNTIME_PACKAGES, manifest["packages"]):
        _check_deadline(deadline)
        if not isinstance(item, dict):
            raise RuntimeAssetError("asset_manifest_invalid")
        if any(
            item.get(key) != expected
            for key, expected in (
                ("name", package.name),
                ("filename", package.filename),
                ("url", package.url),
                ("sha256", package.sha256),
            )
        ):
            raise RuntimeAssetError("asset_manifest_invalid")
        size = item.get("size")
        if not isinstance(size, int) or size <= 0 or size > MAX_PACKAGE_BYTES:
            raise RuntimeAssetError("asset_manifest_invalid")
        package_path = asset_root / package.filename
        try:
            if package_path.is_symlink() or not package_path.is_file():
                raise RuntimeAssetError("asset_missing")
            if package_path.stat().st_size != size:
                raise RuntimeAssetError("asset_checksum")
            if _file_sha256(package_path, deadline=deadline).lower() != package.sha256.lower():
                raise RuntimeAssetError("asset_checksum")
        except TimeoutError:
            raise
        except FileNotFoundError as exc:
            raise RuntimeAssetError("asset_missing") from exc
        except OSError as exc:
            raise RuntimeAssetError("asset_missing") from exc
        verified.append((package, package_path))
    return hashlib.sha256(manifest_bytes).hexdigest(), tuple(verified)


def _platform_fingerprint(system: str, machine: str) -> str:
    libc_name, libc_version = platform.libc_ver()
    return f"{system.lower()}-{machine.lower()}-{libc_name.lower()}-{libc_version}"


@contextlib.contextmanager
def _runtime_file_lock(lock_path: Path, deadline: float):
    """Acquire an OS-level extraction lock, honoring the caller's deadline."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("runtime file lock deadline exceeded")
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                time.sleep(min(0.05, remaining))
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _library_hashes(runtime_root: Path, *, deadline: float | None = None) -> dict[str, str]:
    library_dir = _library_dir(runtime_root)
    hashes = {}
    for name in REQUIRED_RUNTIME_LIBRARY_NAMES:
        _check_deadline(deadline)
        hashes[name] = _file_sha256(library_dir / name, deadline=deadline)
    return hashes


def _safe_remove_owned_directory(path: Path, runtime_root: Path, *, expected_prefix: str) -> None:
    root = runtime_root.resolve()
    if path.parent.resolve() != root or not path.name.startswith(expected_prefix):
        raise RuntimeError("refusing to remove path outside runtime staging root")
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _publish_runtime(
    *,
    runtime_root: Path,
    manifest_digest: str,
    platform_fingerprint: str,
    assets: tuple[tuple[RuntimePackage, Path], ...],
    extractor: Callable[[Path, Path], None],
    deadline: float,
) -> bool:
    runtime_root.mkdir(parents=True, exist_ok=True)
    final_dir = runtime_root / manifest_digest
    if _vendor_libraries_ready(
        final_dir,
        manifest_digest=manifest_digest,
        platform_fingerprint=platform_fingerprint,
        deadline=deadline,
    ):
        return True

    staging = Path(tempfile.mkdtemp(prefix=f"{manifest_digest}.staging-", dir=runtime_root))
    stale: Path | None = None
    try:
        for package, package_path in assets:
            _check_deadline(deadline)
            if extractor is _extract_deb:
                _extract_deb(package_path, staging, deadline=deadline)
            else:
                extractor(package_path, staging)
        if not _vendor_library_files_present(staging):
            raise RuntimeAssetError("asset_extract")
        metadata = {
            "format": 1,
            "manifest_digest": manifest_digest,
            "platform": platform_fingerprint,
            "libraries": _library_hashes(staging, deadline=deadline),
        }
        marker = staging / ".ready.json"
        with marker.open("xb") as stream:
            stream.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if not _vendor_libraries_ready(
            staging,
            manifest_digest=manifest_digest,
            platform_fingerprint=platform_fingerprint,
            deadline=deadline,
        ):
            raise RuntimeAssetError("asset_extract")

        if final_dir.exists() or final_dir.is_symlink():
            stale = runtime_root / f"{manifest_digest}.stale-{uuid.uuid4().hex}"
            final_dir.rename(stale)
        try:
            staging.rename(final_dir)
            staging = None  # type: ignore[assignment]
        except Exception:
            if stale is not None and stale.exists() and not final_dir.exists():
                stale.rename(final_dir)
                stale = None
            raise
        if stale is not None:
            _safe_remove_owned_directory(stale, runtime_root, expected_prefix=f"{manifest_digest}.stale-")
            stale = None
        return False
    finally:
        if staging is not None and staging.exists():
            _safe_remove_owned_directory(
                staging,
                runtime_root,
                expected_prefix=f"{manifest_digest}.staging-",
            )
        if stale is not None and stale.exists():
            # A prior runtime was only moved aside if a fully validated stage
            # was ready to take its place; preserve it if publication failed.
            if not final_dir.exists():
                stale.rename(final_dir)


def _ar_data_member(package_path: Path) -> bytes:
    return _ar_data_member_bytes(package_path.read_bytes())


def _ar_data_member_bytes(raw: bytes) -> bytes:
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


def _extract_deb(package_path: Path, destination: Path, *, deadline: float | None = None) -> None:
    """Extract only x86_64 shared libraries from a .deb without root access."""

    destination.mkdir(parents=True, exist_ok=True)
    _check_deadline(deadline)
    raw_chunks = []
    with package_path.open("rb") as stream:
        while True:
            _check_deadline(deadline)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            raw_chunks.append(chunk)
    package_bytes = b"".join(raw_chunks)
    data_archive = _ar_data_member_bytes(package_bytes)
    destination_root = destination.resolve()
    with tarfile.open(fileobj=io.BytesIO(data_archive), mode="r:*") as archive:
        for member in archive:
            _check_deadline(deadline)
            normalized = _safe_tar_member_name(member.name)
            if normalized.startswith("usr/lib/x86_64-linux-gnu/"):
                extracted_name = normalized
            elif normalized.startswith("lib/x86_64-linux-gnu/"):
                # Debian's merged-/usr packages may still store essential
                # libraries under /lib. Keep the runtime self-contained by
                # placing those files in the same private library directory.
                extracted_name = f"usr/{normalized}"
            else:
                continue
            target = (destination / extracted_name).resolve()
            if not target.is_relative_to(destination_root):
                raise RuntimeError("unsafe library path in Debian package")
            if member.issym() or member.islnk():
                link_target = (target.parent / member.linkname).resolve()
                if not link_target.is_relative_to(destination_root):
                    raise RuntimeError("unsafe library link in Debian package")
            member.name = extracted_name
            archive.extract(member, path=destination)
            _check_deadline(deadline)


def _runtime_failure(
    env: dict[str, str],
    lib_dir: Path,
    message: str,
    *,
    error_code: str,
    manifest_digest: str = "",
) -> PlaywrightRuntimeResult:
    return PlaywrightRuntimeResult(
        ok=False,
        env=env,
        lib_dir=lib_dir,
        message=message,
        error_code=error_code,
        manifest_digest=manifest_digest,
    )


def prepare_playwright_runtime(
    *,
    env: Mapping[str, str] | None = None,
    runtime_root: Path | str | None = None,
    asset_root: Path | str | None = None,
    platform_name: str | None = None,
    machine_name: str | None = None,
    library_checker: Callable[[str], bool] | None = None,
    extractor: Callable[[Path, Path], None] | None = None,
    apply_to_process_env: bool = False,
    deadline: float | None = None,
) -> PlaywrightRuntimeResult:
    """Verify bundled assets and prepare a child environment without apt/network."""

    child_env = dict(os.environ if env is None else env)
    system = (platform_name or platform.system()).lower()
    machine = (machine_name or platform.machine()).lower()
    runtime_base = Path(runtime_root) if runtime_root is not None else DEFAULT_RUNTIME_ROOT
    asset_path = Path(asset_root) if asset_root is not None else DEFAULT_ASSET_ROOT
    budget_deadline = deadline if deadline is not None else time.monotonic() + 120
    try:
        _check_deadline(budget_deadline)
    except TimeoutError:
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            "runtime preparation deadline exceeded",
            error_code="bootstrap_timeout",
        )

    if system != "linux":
        return PlaywrightRuntimeResult(True, child_env, _library_dir(runtime_base), "Linux libraries not required")
    if machine not in {"x86_64", "amd64"}:
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            "unsupported Linux architecture",
            error_code="profile_unsupported",
        )

    try:
        manifest_digest, assets = _load_verified_assets(asset_path, deadline=budget_deadline)
    except RuntimeAssetError as exc:
        public_message = {
            "asset_missing": "runtime asset bundle is missing",
            "asset_checksum": "runtime asset checksum validation failed",
            "asset_manifest_invalid": "runtime asset manifest is invalid",
        }.get(exc.code, "runtime assets could not be validated")
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            public_message,
            error_code=exc.code,
        )
    except TimeoutError:
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            "runtime preparation deadline exceeded",
            error_code="bootstrap_timeout",
        )
    except Exception:
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            "runtime assets could not be validated",
            error_code="bootstrap_unknown",
        )

    checker = library_checker or _library_available
    system_libraries_available = True
    try:
        for name in REQUIRED_RUNTIME_LIBRARY_NAMES:
            _check_deadline(budget_deadline)
            if not checker(name):
                system_libraries_available = False
                break
    except TimeoutError:
        return _runtime_failure(
            child_env,
            _library_dir(runtime_base),
            "runtime preparation deadline exceeded",
            error_code="bootstrap_timeout",
            manifest_digest=manifest_digest,
        )
    if system_libraries_available:
        return PlaywrightRuntimeResult(
            True,
            child_env,
            _library_dir(runtime_base),
            "system libraries available",
            manifest_digest=manifest_digest,
        )

    platform_fingerprint = _platform_fingerprint(system, machine)
    digest_root = runtime_base / manifest_digest
    reused = False
    try:
        remaining = budget_deadline - time.monotonic()
        if remaining <= 0 or not _RUNTIME_LOCK.acquire(timeout=remaining):
            raise TimeoutError("runtime process lock deadline exceeded")
        try:
            with _runtime_file_lock(runtime_base / ".runtime.lock", budget_deadline):
                reused = _publish_runtime(
                    runtime_root=runtime_base,
                    manifest_digest=manifest_digest,
                    platform_fingerprint=platform_fingerprint,
                    assets=assets,
                    extractor=extractor or _extract_deb,
                    deadline=budget_deadline,
                )
        finally:
            _RUNTIME_LOCK.release()
    except TimeoutError:
        return _runtime_failure(
            child_env,
            _library_dir(digest_root),
            "runtime preparation deadline exceeded",
            error_code="bootstrap_timeout",
            manifest_digest=manifest_digest,
        )
    except RuntimeAssetError as exc:
        return _runtime_failure(
            child_env,
            _library_dir(digest_root),
            "runtime libraries could not be prepared",
            error_code=exc.code,
            manifest_digest=manifest_digest,
        )
    except Exception:
        return _runtime_failure(
            child_env,
            _library_dir(digest_root),
            "runtime libraries could not be prepared",
            error_code="asset_extract",
            manifest_digest=manifest_digest,
        )

    lib_dir = _library_dir(digest_root)
    try:
        libraries_ready = _vendor_libraries_ready(
            digest_root,
            manifest_digest=manifest_digest,
            platform_fingerprint=platform_fingerprint,
            deadline=budget_deadline,
        )
    except TimeoutError:
        return _runtime_failure(
            child_env,
            lib_dir,
            "runtime preparation deadline exceeded",
            error_code="bootstrap_timeout",
            manifest_digest=manifest_digest,
        )
    if not libraries_ready:
        return _runtime_failure(
            child_env,
            lib_dir,
            "runtime libraries failed readiness validation",
            error_code="asset_extract",
            manifest_digest=manifest_digest,
        )
    child_env["LD_LIBRARY_PATH"] = _prepend_library_path(lib_dir, child_env.get("LD_LIBRARY_PATH", ""))
    if apply_to_process_env:
        os.environ["LD_LIBRARY_PATH"] = child_env["LD_LIBRARY_PATH"]
    return PlaywrightRuntimeResult(
        ok=True,
        env=child_env,
        lib_dir=lib_dir,
        message="vendored runtime libraries ready",
        reused=reused,
        manifest_digest=manifest_digest,
    )


# The manifest is the version-controlled package lock. Its digest is pinned above
# so a changed package list or file cannot silently become the runtime contract.
def _load_required_runtime_packages() -> tuple[RuntimePackage, ...]:
    try:
        manifest_bytes = (DEFAULT_ASSET_ROOT / "manifest.json").read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != EXPECTED_RUNTIME_MANIFEST_SHA256:
            return ()
        manifest = json.loads(manifest_bytes)
        entries = manifest.get("packages") if isinstance(manifest, dict) else None
        if not isinstance(entries, list):
            return ()
        packages = []
        for item in entries:
            if not isinstance(item, dict):
                return ()
            values = tuple(item.get(key) for key in ("name", "filename", "url", "sha256"))
            if any(not isinstance(value, str) or not value for value in values):
                return ()
            packages.append(RuntimePackage(*values))
        filenames = [package.filename for package in packages]
        if not packages or len(filenames) != len(set(filenames)):
            return ()
        return tuple(packages)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()


REQUIRED_RUNTIME_PACKAGES = _load_required_runtime_packages()



REQUIRED_RUNTIME_LIBRARY_NAMES = (
    "libglib-2.0.so.0",
    "libgobject-2.0.so.0",
    "libgio-2.0.so.0",
    "libnspr4.so",
    "libnss3.so",
    "libnssutil3.so",
    "libsmime3.so",
    "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0",
    "libcairo.so.2",
    "libcups.so.2",
    "libdbus-1.so.3",
    "libX11.so.6",
    "libX11-xcb.so.1",
    "libXcomposite.so.1",
    "libXdamage.so.1",
    "libEGL.so.1",
    "libXext.so.6",
    "libXfixes.so.3",
    "libXrandr.so.2",
    "libgbm.so.1",
    "libgtk-3.so.0",
    "libexpat.so.1",
    "libxcb.so.1",
    "libxkbcommon.so.0",
    "libudev.so.1",
    "libasound.so.2",
    "libatspi.so.0",
    "libXi.so.6",
    "libXrender.so.1",
    "libdrm.so.2",
    "libwayland-server.so.0",
    "libpango-1.0.so.0",
    "libpangocairo-1.0.so.0",
    "libpangoft2-1.0.so.0",
    "libXau.so.6",
    "libXdmcp.so.6",
    "libpcre2-8.so.0",
    "libffi.so.8",
    "libmount.so.1",
    "libselinux.so.1",
    "libsystemd.so.0",
    "libcap.so.2",
    "libblkid.so.1",
    "libgcrypt.so.20",
    "liblz4.so.1",
    "liblzma.so.5",
    "libzstd.so.1",
    "libbsd.so.0",
    "libgpg-error.so.0",
    "libmd.so.0",
    "libxxhash.so.0",
    "libatomic.so.1",
    "libelf.so.1",
    "libedit.so.2",
    "libxml2.so.2",
    "libxcb-dri3.so.0",
    "libxcb-shm.so.0",
    "libxcb-present.so.0",
    "libxcb-randr.so.0",
    "libxcb-sync.so.1",
    "libxcb-xfixes.so.0",
    "libxshmfence.so.1",
    "libsensors.so.5",
)

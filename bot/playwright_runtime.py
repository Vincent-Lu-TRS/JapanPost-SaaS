"""User-space Linux runtime libraries required by Playwright Chromium.

Streamlit Community Cloud can temporarily fail before the app starts when its
``packages.txt`` apt index is stale.  Chromium still needs a chain of shared
libraries, so this module downloads hash-pinned Debian packages and extracts
only their x86_64 library files into ``/tmp``.  No root permission or apt
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


DEFAULT_RUNTIME_ROOT = Path(tempfile.gettempdir()) / "jppost-playwright-runtime"
MAX_PACKAGE_BYTES = 25 * 1024 * 1024


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


# The first deployed version of this helper only covered the three libraries
# named by the original Cloud traceback.  The manifest below covers the full
# headless-shell dependency chain.  Keeping the data at module scope makes the
# package set explicit, reviewable, and shared by every call site.
RUNTIME_VERSION = "2026-09-09-t2"
_RUNTIME_PACKAGE_SPECS = (
    ("libasound2t64", "libasound2t64_1.2.14-1_amd64.deb", "https://deb.debian.org/debian/pool/main/a/alsa-lib/libasound2t64_1.2.14-1_amd64.deb", "f03a2bd9d234f4e6d283c36520d66befd0952d6f5ba454badd8fae2305ad70a1"),
    ("libatk-bridge2.0-0t64", "libatk-bridge2.0-0t64_2.56.2-1+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/a/at-spi2-core/libatk-bridge2.0-0t64_2.56.2-1+deb13u1_amd64.deb", "75a0258ccf7f1aa038e6da50779790dc53d8bd20c525356548056a59b3e4735b"),
    ("libatk1.0-0t64", "libatk1.0-0t64_2.56.2-1+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/a/at-spi2-core/libatk1.0-0t64_2.56.2-1+deb13u1_amd64.deb", "34d443a2927f53d87daf918e342cb40c937d58df5352df5dab3a6cae1dba81f8"),
    ("libatomic1", "libatomic1_14.2.0-19_amd64.deb", "https://deb.debian.org/debian/pool/main/g/gcc-14/libatomic1_14.2.0-19_amd64.deb", "212b399aae2f7299203d261a57e49372e09565a9a5ea971905f94a3960366c05"),
    ("libatspi2.0-0t64", "libatspi2.0-0t64_2.56.2-1+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/a/at-spi2-core/libatspi2.0-0t64_2.56.2-1+deb13u1_amd64.deb", "2f79dc0e8117b07ae0e0cd373079d4f383a6ec701a2ffcc0b085216dc4abea52"),
    ("libblkid1", "libblkid1_2.41-5_amd64.deb", "https://deb.debian.org/debian/pool/main/u/util-linux/libblkid1_2.41-5_amd64.deb", "bed93f11942524e5338074c46cce376197740635bf0745ccb7d259e0f6ce7117"),
    ("libbsd0", "libbsd0_0.12.2-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libb/libbsd/libbsd0_0.12.2-2_amd64.deb", "e5a85986fa6bec3307ab1bc860736b478b331882bc45e17675a7bdf88eecb43a"),
    ("libcap2", "libcap2_2.75-10+deb13u1+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libc/libcap2/libcap2_2.75-10+deb13u1+b1_amd64.deb", "860db6f80d400239744eee2b454f1d8e66b064e18dd0527535824887691e9cb7"),
    ("libdbus-1-3", "libdbus-1-3_1.16.2-2_amd64.deb", "https://deb.debian.org/debian/pool/main/d/dbus/libdbus-1-3_1.16.2-2_amd64.deb", "bbcb711daff7e104b5f80f9b05475b6142674bfe65518a36a56e96a91068a3f5"),
    ("libdrm-amdgpu1", "libdrm-amdgpu1_2.4.124-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libd/libdrm/libdrm-amdgpu1_2.4.124-2_amd64.deb", "c1d97a5e32e2bc68e833b8abeae026b6306198e87ee4396a7ffecd4779caefbf"),
    ("libdrm-intel1", "libdrm-intel1_2.4.124-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libd/libdrm/libdrm-intel1_2.4.124-2_amd64.deb", "188ab1fd74c838b3c8055d62107351e16a4bb7a25d39c30bbb9aac30ddd37238"),
    ("libdrm2", "libdrm2_2.4.124-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libd/libdrm/libdrm2_2.4.124-2_amd64.deb", "fe2276901c7cd7b8079de63072d37fe1cbeb4eb001a3bc1f1d662ad89aa0890e"),
    ("libedit2", "libedit2_3.1-20250104-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libe/libedit/libedit2_3.1-20250104-1_amd64.deb", "b002ea172b9c1e34a67bc497c523c67bb74c3f0a4e98113cb083990a1f1d3bfe"),
    ("libelf1t64", "libelf1t64_0.192-4_amd64.deb", "https://deb.debian.org/debian/pool/main/e/elfutils/libelf1t64_0.192-4_amd64.deb", "94497b7e17b6f574a0605b380d454e20d3f01a9c63b70c2a2263f679d30053e1"),
    ("libexpat1", "libexpat1_2.7.1-2_amd64.deb", "https://deb.debian.org/debian/pool/main/e/expat/libexpat1_2.7.1-2_amd64.deb", "f875f56675be5b074da877f9a93b09d47dc2eb4e679951d36e2943b8d4843344"),
    ("libffi8", "libffi8_3.4.8-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libf/libffi/libffi8_3.4.8-2_amd64.deb", "0ebdc340de33333639c3c63874cd4b15ac2e83dfa1ef3053b7eefaf4919f4f68"),
    ("libgbm1", "libgbm1_25.0.7-2+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/m/mesa/libgbm1_25.0.7-2+deb13u1_amd64.deb", "31fb6d76b9ceaf13848fa617df53f85f62626b4fe7464a93811c720af6d5f2dd"),
    ("libgcrypt20", "libgcrypt20_1.11.0-7+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/libg/libgcrypt20/libgcrypt20_1.11.0-7+deb13u1_amd64.deb", "f0eb41f8cb40896e0822e9ab65611e8383c12b721c76c54764f744abb4a8c247"),
    ("libglib2.0-0t64", "libglib2.0-0t64_2.84.4-3~deb13u3_amd64.deb", "https://deb.debian.org/debian/pool/main/g/glib2.0/libglib2.0-0t64_2.84.4-3~deb13u3_amd64.deb", "e7bc3813fa7effa8bbd0d5547e6198ba18f53f5e79d305b8c34e8dce27f6c454"),
    ("libgpg-error0", "libgpg-error0_1.51-4_amd64.deb", "https://deb.debian.org/debian/pool/main/libg/libgpg-error/libgpg-error0_1.51-4_amd64.deb", "22b95570fd41c113ef6f5651563b4d748292844baab1278a46eb940c1dec2322"),
    ("libllvm19", "libllvm19_19.1.7-3+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/l/llvm-toolchain-19/libllvm19_19.1.7-3+b1_amd64.deb", "db0d614d61345ca710fb73e75105f1ec6e38898fa7b3aa99fa7eb7d8b0a11d57"),
    ("liblzma5", "liblzma5_5.8.1-1+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/x/xz-utils/liblzma5_5.8.1-1+deb13u1_amd64.deb", "1cfcc6e0dc36f438a79b6e2189facdb9d150b08f57d190a60e01c98075c7f896"),
    ("liblz4-1", "liblz4-1_1.10.0-4_amd64.deb", "https://deb.debian.org/debian/pool/main/l/lz4/liblz4-1_1.10.0-4_amd64.deb", "c31ec4c7c82755a38b2f3fe066fc0c5518cc91a601a268fbcd19bdacb1f22e1e"),
    ("libmd0", "libmd0_1.1.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libm/libmd/libmd0_1.1.0-2+b1_amd64.deb", "7244ec3839b61fac0c1884fe08aaa040f26e8f1f35f1f5d3482eacefd30d1b44"),
    ("libmount1", "libmount1_2.41-5_amd64.deb", "https://deb.debian.org/debian/pool/main/u/util-linux/libmount1_2.41-5_amd64.deb", "cd9efc7addd2556062f6d148b502e38a64e08c0ab220806219cfffd865f0f6f6"),
    ("libnspr4", "libnspr4_4.36-1_amd64.deb", "https://deb.debian.org/debian/pool/main/n/nspr/libnspr4_4.36-1_amd64.deb", "87fc2039ee89cff2b8010fa9535706b1def40031acfc52bf12197dcb3cb7b064"),
    ("libnss3", "libnss3_3.110-1+deb13u3_amd64.deb", "https://deb.debian.org/debian/pool/main/n/nss/libnss3_3.110-1+deb13u3_amd64.deb", "9f4fb2f6bc2530529dd3e79c411566de696fae1ddeb8d585756cc92543bbcb6b"),
    ("libpciaccess0", "libpciaccess0_0.17-3+b3_amd64.deb", "https://deb.debian.org/debian/pool/main/libp/libpciaccess/libpciaccess0_0.17-3+b3_amd64.deb", "d9a0091071635a84e837051e4813005ac445071731becea28fba4d9806df5252"),
    ("libpcre2-8-0", "libpcre2-8-0_10.46-1~deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/p/pcre2/libpcre2-8-0_10.46-1~deb13u1_amd64.deb", "6aa452af6a07e34498bf8e734e8789c9dd602fb34f12572e62dc1bba4889a60e"),
    ("libselinux1", "libselinux1_3.8.1-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libs/libselinux/libselinux1_3.8.1-1_amd64.deb", "68bb8d32bd8d6d7d2f5952a169db03d1484b46ae1e52abccdec42a19dccea5d5"),
    ("libsensors5", "libsensors5_3.6.2-2_amd64.deb", "https://deb.debian.org/debian/pool/main/l/lm-sensors/libsensors5_3.6.2-2_amd64.deb", "f0a994a6d7cfa695dea5343d0d1ba7eed796c0ad920c7282998b95f60049c4f6"),
    ("libsqlite3-0", "libsqlite3-0_3.46.1-7+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/s/sqlite3/libsqlite3-0_3.46.1-7+deb13u1_amd64.deb", "edeacbdd6a86ee82d314dccc180b740905fd7f6392b72e5c0533079fda449361"),
    ("libsystemd0", "libsystemd0_257.13-1~deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/s/systemd/libsystemd0_257.13-1~deb13u1_amd64.deb", "ab0d4127b5e46e6f8c015a1db15a62ba9ae274cdefa150083adf90ada0600ea1"),
    ("libudev1", "libudev1_257.13-1~deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/s/systemd/libudev1_257.13-1~deb13u1_amd64.deb", "5d41c284f5a93b05bc7d648b61a02dd2bb9ff05b2261ad1a8b7d96044a0cfa88"),
    ("libwayland-server0", "libwayland-server0_1.23.1-3_amd64.deb", "https://deb.debian.org/debian/pool/main/w/wayland/libwayland-server0_1.23.1-3_amd64.deb", "2967212bd582e0dffca443fdc44f4c660e7368d41f7ee3a7f6314e0c3abfe9ea"),
    ("libx11-6", "libx11-6_1.8.12-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libx11/libx11-6_1.8.12-1_amd64.deb", "b5a3fd3bf8c8fd0364bfb9bea00dcba7fc301229bd02dded084632d31f5b0fb3"),
    ("libx11-xcb1", "libx11-xcb1_1.8.12-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libx11/libx11-xcb1_1.8.12-1_amd64.deb", "e05f94d21a932fba5b09b9b13d99df776b155d3bc792c0e294451df9ffe1ba25"),
    ("libxau6", "libxau6_1.0.11-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxau/libxau6_1.0.11-1_amd64.deb", "689a9f0e0ba3e2c65431f864871e303ee904de69dd28abfc462663fae030227f"),
    ("libxcb-dri3-0", "libxcb-dri3-0_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb-dri3-0_1.17.0-2+b1_amd64.deb", "f446d42fb5fcebbb3e347368ba83616769fdb271d85b2f49048e337f3163d267"),
    ("libxcb-present0", "libxcb-present0_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb-present0_1.17.0-2+b1_amd64.deb", "db95ea4630c55bd7f3281cb60ccf75b1627ef2f4399e1939f8f6161e584a92fb"),
    ("libxcb-randr0", "libxcb-randr0_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb-randr0_1.17.0-2+b1_amd64.deb", "f5d9fe5fdf797918f81f0abf6cfd4270bd54659540cddb4da709ab7154523214"),
    ("libxcb-sync1", "libxcb-sync1_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb-sync1_1.17.0-2+b1_amd64.deb", "0ce4770ed1505be9ddc6473045e4662d062aff2f3077ee684265f01cfb543559"),
    ("libxcb-xfixes0", "libxcb-xfixes0_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb-xfixes0_1.17.0-2+b1_amd64.deb", "f9c1aafc18bf9e4662e34bbaf3bb00f1f4e8c2fc1dd786fdfb6cb9cb6c64fae7"),
    ("libxcb1", "libxcb1_1.17.0-2+b1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcb/libxcb1_1.17.0-2+b1_amd64.deb", "5c222a72d11b866447da31693254f738430726e3e065a384e82687b2fd2f978b"),
    ("libxcomposite1", "libxcomposite1_0.4.6-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxcomposite/libxcomposite1_0.4.6-1_amd64.deb", "20e3c1d9b2135f0c8c4246a9fd26a51a57d5850c3b36ecc173c45d6be7328af3"),
    ("libxdamage1", "libxdamage1_1.1.6-1+b2_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxdamage/libxdamage1_1.1.6-1+b2_amd64.deb", "e51e43f23f3befbc1f9408271f4df6773d37caece9ce7e1f38abade382f7fbf7"),
    ("libxdmcp6", "libxdmcp6_1.1.5-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxdmcp/libxdmcp6_1.1.5-1_amd64.deb", "0740dc760916b2008b45417a42a8fd7dd5de370fb57d31373f15034cda8acf0b"),
    ("libxext6", "libxext6_1.3.4-1+b3_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxext/libxext6_1.3.4-1+b3_amd64.deb", "fc618ec40465e5ce48622606299cb47833efc3fb235ba15543b81f850722f443"),
    ("libxfixes3", "libxfixes3_6.0.0-2+b4_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxfixes/libxfixes3_6.0.0-2+b4_amd64.deb", "3fdd95d86d8e9d63e11f52070935c8f9f912c36aa3b17338d969f7685f3ed4fb"),
    ("libxi6", "libxi6_1.8.2-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxi/libxi6_1.8.2-1_amd64.deb", "093d0903f35bb7a9f6815180ee040e6951fecf9b66c128cd72f064710210606e"),
    ("libxkbcommon0", "libxkbcommon0_1.7.0-2_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxkbcommon/libxkbcommon0_1.7.0-2_amd64.deb", "f75ee544f55acc6a271debfab3ea4ae0458afc89d81cfe1a71137e07d4895b86"),
    ("libxml2", "libxml2_2.12.7+dfsg+really2.9.14-2.1+deb13u3_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxml2/libxml2_2.12.7+dfsg+really2.9.14-2.1+deb13u3_amd64.deb", "e0c6b63ce4602a036a526f60fe5e6c1586710688058d98fc1001b9b3147b7efd"),
    ("libxrandr2", "libxrandr2_1.5.4-1+b3_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxrandr/libxrandr2_1.5.4-1+b3_amd64.deb", "11e3490de93a8bbee3daba719cb8e1325a26fb3c125525c34bdcb7deb05eb9b2"),
    ("libxrender1", "libxrender1_0.9.12-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxrender/libxrender1_0.9.12-1_amd64.deb", "9d042dfd5e613be1e02e6ddd0c5c4adef19c5eb08f6db838c2eba672c496dca4"),
    ("libxshmfence1", "libxshmfence1_1.3.3-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxshmfence/libxshmfence1_1.3.3-1_amd64.deb", "7b339e9e5b2349723d35af4df89bcc7aa456bbdf8ba1754358f9b44c3fe1f964"),
    ("libxss1", "libxss1_1.2.3-1+b3_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxss/libxss1_1.2.3-1+b3_amd64.deb", "302d4a97800429fa0d67ae2e0e8e3adc1228b12d25d3d3cca6c9d24eeff40a6c"),
    ("libxtst6", "libxtst6_1.2.5-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libx/libxtst/libxtst6_1.2.5-1_amd64.deb", "4f254f80b8984203474745d6428e8c2de2148f19b37aa01a601dbe9ac03196eb"),
    ("libxxhash0", "libxxhash0_0.8.3-2_amd64.deb", "https://deb.debian.org/debian/pool/main/x/xxhash/libxxhash0_0.8.3-2_amd64.deb", "81da7064d56fc044f5db4bb3c1e80ff50a4986dcbbf80d958eeca565b44c157f"),
    ("libz3-4", "libz3-4_4.13.3-1_amd64.deb", "https://deb.debian.org/debian/pool/main/z/z3/libz3-4_4.13.3-1_amd64.deb", "71383373523ef62d47eccf660cf6535c3febcbb3f88e54cb7a43124014b57359"),
    ("libzstd1", "libzstd1_1.5.7+dfsg-1_amd64.deb", "https://deb.debian.org/debian/pool/main/libz/libzstd/libzstd1_1.5.7+dfsg-1_amd64.deb", "2f6a2aeacfc925eba8b00ac9139bc4bfccf8cacb09eb93de067074b26948eef9"),
    ("mesa-libgallium", "mesa-libgallium_25.0.7-2+deb13u1_amd64.deb", "https://deb.debian.org/debian/pool/main/m/mesa/mesa-libgallium_25.0.7-2+deb13u1_amd64.deb", "3e610f29321cdcc61337c86e6b4031ff60f0c9915fe19e6a862ac06480620ff4"),
)

REQUIRED_RUNTIME_PACKAGES = tuple(RuntimePackage(*spec) for spec in _RUNTIME_PACKAGE_SPECS)

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
    "libdbus-1.so.3",
    "libX11.so.6",
    "libXcomposite.so.1",
    "libXdamage.so.1",
    "libXext.so.6",
    "libXfixes.so.3",
    "libXrandr.so.2",
    "libgbm.so.1",
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
    "libxcb-present.so.0",
    "libxcb-randr.so.0",
    "libxcb-sync.so.1",
    "libxcb-xfixes.so.0",
    "libxshmfence.so.1",
    "libsensors.so.5",
)

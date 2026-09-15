"""Build an immutable, hash-verified bundle of Playwright runtime .deb files."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import shlex
import sys
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable
from urllib.parse import unquote, urlsplit, urlunsplit


MAX_PACKAGE_BYTES = 25 * 1024 * 1024
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "vendor"
    / "playwright-runtime-v1-linux-x86_64"
)


def _publish_directory_no_replace(stage: Path, output: Path) -> None:
    """Atomically publish a staged directory only if the destination is absent.

    POSIX ``rename`` may replace an existing empty destination directory. Linux
    therefore needs ``renameat2(RENAME_NOREPLACE)``; Windows ``os.rename`` is
    already non-replacing. Unsupported platforms fail closed rather than
    falling back to a racy check-then-rename sequence.
    """

    if os.name == "nt":
        os.rename(stage, output)
        return

    if sys.platform.startswith("linux"):
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace directory publish is unavailable")
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,  # AT_FDCWD
            os.fsencode(stage),
            -100,
            os.fsencode(output),
            1,  # RENAME_NOREPLACE
        )
        if result == 0:
            return
        error_code = ctypes.get_errno()
        raise OSError(error_code, os.strerror(error_code), str(stage), str(output))

    raise OSError(errno.ENOTSUP, "atomic no-replace directory publish is unsupported")


def _canonical_manifest(packages: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"format": 1, "platform": "linux-x86_64", "packages": packages},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_asset_bundle(
    source_dir: str | Path,
    output_dir: str | Path,
    *,
    packages: Iterable[object],
) -> str:
    """Verify and atomically publish the exact package set to ``output_dir``.

    Each package object must expose ``name``, ``filename``, ``url`` and
    ``sha256``. This accepts the runtime's pinned package registry while
    remaining straightforward to test with small fixtures.
    """

    source = Path(source_dir)
    output = Path(output_dir)
    parent = output.parent.resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("output already exists")
    if not source.is_dir():
        raise ValueError("source directory is missing")
    output.parent.mkdir(parents=True, exist_ok=True)

    stage: Path | None = None
    try:
        stage = Path(tempfile.mkdtemp(prefix=f"{output.name}.staging-", dir=parent))
        manifest_packages: list[dict[str, object]] = []
        seen: set[str] = set()
        for package in packages:
            filename = str(package.filename)
            if Path(filename).name != filename or not filename.endswith(".deb") or filename in seen:
                raise ValueError("invalid or duplicate package filename")
            seen.add(filename)
            src = source / filename
            if not src.is_file() or src.is_symlink():
                raise ValueError(f"missing package: {filename}")
            target = stage / filename
            digest = hashlib.sha256()
            total = 0
            with src.open("rb") as reader, target.open("xb") as writer:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PACKAGE_BYTES:
                        raise ValueError(f"package exceeds size limit: {filename}")
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest().lower() != str(package.sha256).lower():
                raise ValueError(f"checksum mismatch: {filename}")
            manifest_packages.append(
                {
                    "name": str(package.name),
                    "filename": filename,
                    "url": str(package.url),
                    "size": total,
                    "sha256": digest.hexdigest(),
                }
            )

        if not manifest_packages:
            raise ValueError("package list is empty")
        manifest = _canonical_manifest(manifest_packages)
        manifest_path = stage / "manifest.json"
        with manifest_path.open("xb") as stream:
            stream.write(manifest)
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists() or output.is_symlink():
            raise ValueError("output appeared during build")
        _publish_directory_no_replace(stage, output)
        stage = None
        return hashlib.sha256(manifest).hexdigest()
    finally:
        if stage is not None:
            resolved_stage = stage.resolve()
            if resolved_stage.parent == parent and stage.name.startswith(f"{output.name}.staging-"):
                shutil.rmtree(resolved_stage, ignore_errors=True)


def parse_apt_download_urls(uri_list: str | Path) -> dict[str, str]:
    """Map Debian apt --print-uris entries to canonical HTTPS package URLs."""

    approved_hosts = {"deb.debian.org", "security.debian.org"}
    urls: dict[str, str] = {}
    for line in Path(uri_list).read_text(encoding="utf-8").splitlines():
        try:
            fields = shlex.split(line)
        except ValueError as exc:
            raise ValueError("apt URI list is malformed") from exc
        if not fields or ".deb" not in fields[0].lower():
            continue
        if len(fields) < 2:
            raise ValueError("apt URI entry is missing its saved filename")
        parsed = urlsplit(fields[0])
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in approved_hosts:
            raise ValueError("apt package URI uses an unapproved repository host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("apt package URI contains unsupported components")
        # apt prints both the package URI and the exact local filename it will
        # save. Those names can differ: Debian epochs and URL-escaped tildes
        # are normalized differently in the local filename. Key the map by
        # apt's second field so it matches the downloaded file on disk.
        filename = fields[1]
        decoded_filename = unquote(filename)
        if (
            not filename
            or Path(filename).name != filename
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
            or not filename.lower().endswith(".deb")
            or not decoded_filename.endswith(".deb")
            or "/" in decoded_filename
            or "\\" in decoded_filename
            or "\x00" in decoded_filename
        ):
            raise ValueError("apt package URI has an invalid filename")
        canonical_url = urlunsplit(("https", parsed.netloc.lower(), parsed.path, "", ""))
        previous = urls.get(filename)
        if previous is not None and previous != canonical_url:
            raise ValueError("apt URI list contains conflicting package sources")
        urls[filename] = canonical_url
    return urls


def _parse_deb_package_metadata(output: str) -> tuple[str, str]:
    """Read Package and Architecture fields from dpkg-deb output."""

    fields: dict[str, str] = {}
    positional: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            normalized_key = key.strip().lower()
            if normalized_key in {"package", "architecture"}:
                if normalized_key in fields or not value.strip():
                    raise ValueError("runtime package control fields are invalid")
                fields[normalized_key] = value.strip()
            else:
                raise ValueError("runtime package control fields are unexpected")
        else:
            positional.append(line)

    if fields and not positional and set(fields) == {"package", "architecture"}:
        return fields["package"], fields["architecture"]
    if not fields and len(positional) == 2:
        return positional[0], positional[1]
    raise ValueError("runtime package control fields are invalid")


def packages_from_deb_directory(source_dir: str | Path, uri_list: str | Path) -> tuple[object, ...]:
    """Discover package identity and checksums for an apt-downloaded .deb set."""

    source = Path(source_dir)
    urls = parse_apt_download_urls(uri_list)
    packages = []
    for package_path in sorted(source.glob("*.deb")):
        if package_path.is_symlink() or not package_path.is_file():
            raise ValueError("runtime package input is not a regular file")
        url = urls.get(package_path.name)
        if url is None:
            raise ValueError(f"missing apt source URL: {package_path.name}")
        metadata_output = subprocess.run(
            ["dpkg-deb", "--field", str(package_path), "Package", "Architecture"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        name, architecture = _parse_deb_package_metadata(metadata_output)
        if not name or architecture not in {"amd64", "all"}:
            raise ValueError(f"runtime package architecture is unsupported: {package_path.name}")
        with package_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        packages.append(
            SimpleNamespace(
                name=name,
                filename=package_path.name,
                url=url,
                sha256=digest,
            )
        )
    if not packages:
        raise ValueError("runtime package source is empty")
    return tuple(packages)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory containing the pre-verified .deb inputs")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--apt-uri-list", type=Path, help="apt --print-uris output used to build a new pinned asset bundle")
    args = parser.parse_args()

    import sys

    repository = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository))
    from bot.playwright_runtime import REQUIRED_RUNTIME_PACKAGES

    packages = (
        packages_from_deb_directory(args.source, args.apt_uri_list)
        if args.apt_uri_list is not None
        else REQUIRED_RUNTIME_PACKAGES
    )
    digest = build_asset_bundle(args.source, args.output, packages=packages)
    print(f"published {len(packages)} verified packages; manifest sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

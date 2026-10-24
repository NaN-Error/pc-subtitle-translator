"""Assemble a portable Windows x64 bundle without cross-compiling an executable.

Run with Python 3.12+ and pip on Linux or Windows. Only public CPython binaries
and Windows/pure-Python wheels are downloaded; none are executed on the host.

    python scripts/build_portable.py --download-only
    python scripts/build_portable.py --offline

The output uses the official CPython embeddable runtime. A Windows machine is
still required to validate native audio, Credential Manager, and the overlay.
"""

from __future__ import annotations

import argparse
import email
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile


PYTHON_VERSION = "3.14.9"
PYTHON_ZIP = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ZIP}"
# Published SHA-256 on the official Python 3.14.9 release page, 2026-10-21.
PYTHON_SHA256 = "3c01d7cfc666f746a6f064f60fb2156ab0ec464d3d738ab796371a46a9bb24a2"
PYTHON_RELEASE_URL = "https://www.python.org/downloads/release/python-3149/"
BUILD_NAME = "Sub"
RUNTIME_DIR = "r"
PACKAGES_DIR = "p"
MAX_ARCHIVE_PATH = 128
BUILD_SENTINEL = ".pc-subtitle-translator-portable-build"
ALLOWED_DOWNLOAD_HOSTS = {"www.python.org", "python.org", "pypi.org", "files.pythonhosted.org"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def windows_requirements(project: Path) -> dict[str, tuple[str, str]]:
    """Read the explicitly pinned Windows dependency closure, not host markers."""
    result: dict[str, tuple[str, str]] = {}
    for line in (project / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement, separator, marker = line.partition(";")
        if separator and marker.strip() not in {'sys_platform == "win32"', "sys_platform == 'win32'"}:
            raise ValueError(f"Unsupported marker in portable requirements: {line}")
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", requirement.strip())
        if not match:
            raise ValueError(f"Every portable dependency must have an exact pin: {line}")
        name, version = match.groups()
        normalized = canonical_name(name)
        if normalized in result:
            raise ValueError(f"Duplicate dependency: {name}")
        result[normalized] = (name, version)
    if not result:
        raise ValueError("requirements.txt contains no pinned dependencies")
    return result


def checked_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS or parsed.username:
        raise ValueError(f"Download URL is not an approved public HTTPS source: {url}")
    return url


def fetch_bytes(url: str, max_bytes: int = 16 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(checked_url(url), headers={"User-Agent": "PcSubtitleTranslator-build/0.1"})
    with urllib.request.urlopen(request, timeout=45) as response:
        checked_url(response.url)
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Download exceeds the size limit: {url}")
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)
        if len(payload) > max_bytes:
            raise ValueError(f"Decompressed response exceeds the size limit: {url}")
    return payload


def fetch_metadata(name: str, version: str, cache: Path, offline: bool) -> dict:
    path = cache / "metadata" / f"{canonical_name(name)}-{version}.json"
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Cached PyPI metadata missing: {path}; run without --offline first")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = fetch_bytes(f"https://pypi.org/pypi/{name}/{version}/json")
        metadata = json.loads(payload)
        path.write_bytes(payload)
    else:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    if canonical_name(metadata["info"]["name"]) != canonical_name(name) or metadata["info"]["version"] != version:
        raise ValueError(f"PyPI metadata identity mismatch for {name}=={version}")
    return metadata


def ensure_runtime(cache: Path, offline: bool) -> Path:
    path = cache / PYTHON_ZIP
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"Cached CPython ZIP missing: {path}")
        print(f"Downloading official CPython {PYTHON_VERSION} Windows x64 runtime...", flush=True)
        path.write_bytes(fetch_bytes(PYTHON_URL, max_bytes=32 * 1024 * 1024))
    if sha256(path) != PYTHON_SHA256:
        raise ValueError(f"CPython SHA-256 mismatch for {path}; remove this cache file and retry")
    return path


def wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        candidates = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(candidates) != 1:
            raise ValueError(f"Expected one wheel METADATA file: {path.name}")
        metadata = email.message_from_bytes(archive.read(candidates[0]))
        return canonical_name(str(metadata["Name"])), str(metadata["Version"])


def find_requested_wheels(directory: Path, requirements: dict[str, tuple[str, str]]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*.whl")):
        name, version = wheel_identity(path)
        if name not in requirements or version != requirements[name][1]:
            continue
        if name in found:
            raise ValueError(f"Multiple cached wheels for {name}; use a fresh --cache-dir")
        found[name] = path
    return found


def ensure_wheels(project: Path, cache: Path, offline: bool) -> list[dict]:
    requirements = windows_requirements(project)
    wheel_directory = cache / "wheels"
    wheel_directory.mkdir(parents=True, exist_ok=True)
    found = find_requested_wheels(wheel_directory, requirements)
    missing = [f"{name}=={version}" for normalized, (name, version) in requirements.items() if normalized not in found]
    if missing:
        if offline:
            raise FileNotFoundError(f"Cached wheels missing: {', '.join(missing)}")
        print("Downloading exact Windows x64 CPython 3.14 wheels...", flush=True)
        subprocess.run(
            [
                sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check",
                "download", "--index-url", "https://pypi.org/simple", "--no-deps",
                "--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp",
                "--python-version", "3.14", "--abi", "cp314", "--no-cache-dir",
                "--dest", str(wheel_directory), *missing,
            ],
            check=True,
        )
        found = find_requested_wheels(wheel_directory, requirements)
    if set(found) != set(requirements):
        raise ValueError("The downloaded wheels do not cover the pinned Windows dependencies")
    records: list[dict] = []
    for normalized, path in sorted(found.items()):
        name, version = requirements[normalized]
        metadata = fetch_metadata(name, version, cache, offline)
        files = [entry for entry in metadata["urls"] if entry["filename"] == path.name]
        if len(files) != 1 or files[0].get("yanked"):
            raise ValueError(f"Wheel is missing from PyPI or yanked: {path.name}")
        entry = files[0]
        checked_url(entry["url"])
        digest = sha256(path)
        if digest != entry["digests"]["sha256"] or path.stat().st_size != entry["size"]:
            raise ValueError(f"Wheel SHA-256 or size mismatch: {path.name}")
        # Require a Windows x64 or architecture-independent wheel. pip performs
        # the complete CPython/abi3 compatibility selection before this check.
        if not (path.name.endswith("-win_amd64.whl") or path.name.endswith("-none-any.whl")):
            raise ValueError(f"Unexpected platform wheel: {path.name}")
        records.append({
            "name": name, "version": version, "filename": path.name,
            "sha256": digest, "size_bytes": path.stat().st_size, "url": entry["url"],
            "project_urls": metadata["info"].get("project_urls") or {},
        })
    return records


def safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe ZIP entry: {name!r}")
    if not path.parts:
        raise ValueError("Empty ZIP entry")
    return path


def extract_archive(archive_path: Path, target: Path, *, wheel: bool = False, runtime: Path | None = None) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt ZIP: {archive_path.name}")
        for entry in archive.infolist():
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Unexpected symbolic link in {archive_path.name}: {entry.filename}")
            parts = safe_member_path(entry.filename).parts
            destination_root = target
            if wheel and parts[0].endswith(".data"):
                if len(parts) < 2:
                    if entry.is_dir():
                        continue
                    raise ValueError(f"Invalid wheel .data entry: {entry.filename}")
                category = parts[1]
                if category in {"purelib", "platlib"}:
                    destination_root = target
                elif category == "data":
                    destination_root = runtime or target
                elif category == "scripts":
                    destination_root = (runtime or target) / "Scripts"
                elif category == "headers":
                    destination_root = (runtime or target) / "Include"
                else:
                    raise ValueError(f"Unsupported wheel .data category: {category}")
                parts = parts[2:]
                if not parts:
                    continue
            destination = destination_root.joinpath(*parts)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)


def copy_project(project: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", "*.log", ".env*")
    for name in ("pc_subtitle_translator", "scripts", "tests", "docs"):
        path = project / name
        if path.is_dir():
            shutil.copytree(path, target / name, ignore=ignored)
    for path in project.iterdir():
        if path.is_file() and (path.suffix.lower() in {".md", ".txt", ".toml", ".bat"} or path.name == "LICENSE"):
            if not path.name.startswith("."):
                shutil.copy2(path, target / path.name)


def omit_unused_qt_objects(sites: Path) -> list[dict]:
    """Omit four static-build intermediates, keeping every runtime DLL/asset.

    These object files come from the wheel's QML assetdownloader static-build
    support. This app loads the published DLLs and never links a static Qt app.
    Use an exact allowlist rather than broadly removing development directories.
    """
    base = Path("PySide6/qml/Qt/labs/assetdownloader")
    omitted = []
    for flavor in ("Debug", "RelWithDebInfo"):
        for tail in (
            "QmlAssetDownloaderPrivate_resources_1/.qt/rcc/qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj",
            "QmlAssetDownloaderPrivateplugin_init/QmlAssetDownloaderPrivateplugin_init.cpp.obj",
        ):
            path = sites / base / f"objects-{flavor}" / tail
            if path.is_file():
                omitted.append({
                    "path": path.relative_to(sites).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256(path),
                    "reason": "Unused static Qt build object; runtime DLLs and assets retained",
                })
                path.unlink()
                # Empty intermediate directories also count toward Explorer paths.
                parent = path.parent
                while parent != sites and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
    return omitted


def windows_path_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def audit_windows_paths(bundle: Path) -> dict:
    """Fail the build before a ZIP with overly deep or invalid paths is delivered.

    The 128-unit relative budget includes the short top-level directory. Even
    a 120-unit destination prefix leaves the full path below classic MAX_PATH.
    This is a static compatibility check, not a claim of Windows execution.
    """
    names = set()
    longest = ""
    longest_length = 0
    count = 0
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)
    }
    for path in sorted(bundle.rglob("*")):
        relative = path.relative_to(bundle.parent).as_posix()
        if path.is_symlink():
            raise ValueError(f"Symbolic links are not supported in the portable bundle: {relative}")
        key = relative.casefold()
        if key in names:
            raise ValueError(f"Case-insensitive Windows path collision: {relative}")
        names.add(key)
        for component in PurePosixPath(relative).parts:
            if (component.endswith((" ", ".")) or windows_path_units(component) > 255
                    or component.split(".", 1)[0].casefold() in reserved
                    or any(c in '<>:"\\|?*' or ord(c) < 32 for c in component)):
                raise ValueError(f"Invalid Windows path component: {relative}")
        length = windows_path_units(relative)
        if length > MAX_ARCHIVE_PATH:
            raise ValueError(
                f"Portable path exceeds the {MAX_ARCHIVE_PATH}-character budget "
                f"({length}): {relative}"
            )
        if length > longest_length:
            longest, longest_length = relative, length
        count += 1
    return {
        "maximum_relative_path_utf16_units": longest_length,
        "longest_path": longest,
        "entries_checked": count,
        "relative_path_budget": MAX_ARCHIVE_PATH,
        "maximum_full_path_with_120_character_destination": 121 + longest_length,
        "native_windows_extraction_tested": False,
    }


def write_batch(path: Path, contents: str) -> None:
    path.write_bytes(contents.strip().replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8") + b"\r\n")


def write_launchers(bundle: Path) -> None:
    write_batch(bundle / "Start.bat", r"""
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
start "" /wait "%~dp0r\pythonw.exe" -B -m pc_subtitle_translator %*
if not errorlevel 1 exit /b 0
echo The app could not start. Run Debug.bat to see the error.
pause
exit /b 1
""")
    write_batch(bundle / "Debug.bat", r"""
@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0r\python.exe" -B -m pc_subtitle_translator %*
if not errorlevel 1 exit /b 0
echo.
echo The app stopped with an error. Details are above.
pause
exit /b 1
""")


def preserve_licenses(bundle: Path, records: list[dict]) -> None:
    licenses = bundle / "licenses"
    licenses.mkdir()
    runtime = bundle / RUNTIME_DIR
    sites = runtime / PACKAGES_DIR
    if (runtime / "LICENSE.txt").exists():
        shutil.copy2(runtime / "LICENSE.txt", licenses / "Python-LICENSE.txt")
    for directory in sorted(sites.glob("*.dist-info")):
        for path in directory.rglob("*"):
            if path.is_file() and any(token in path.name.lower() for token in ("license", "copying", "notice", "copyright")):
                destination = licenses / directory.name / path.relative_to(directory)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    lines = [
        "Third-party software notices", "============================", "",
        "This portable app includes CPython and files from verified Python distributions.",
        "Retained runtime files are unmodified; four unused Qt static-build objects are omitted.",
        "The omissions are recorded in bundle-manifest.json. Every runtime DLL and asset is retained.",
        "Licenses and copyright notices remain in r/ and each .dist-info directory under r/p/.",
        "Copies of discovered license/notice files are also provided in licenses/.", "",
        f"CPython {PYTHON_VERSION}: {PYTHON_RELEASE_URL}",
        "CPython source: https://www.python.org/downloads/source/", "",
        "Qt for Python (PySide6/Shiboken6) and Qt are separate third-party libraries.",
        "The Qt DLLs and Python bindings remain separate, replaceable files; this package",
        "does not combine them into a single executable or restrict library replacement.",
        "The application source is included in app/. To substitute a compatible modified",
        "library build, replace its files under r/p/ and restart.",
        "Qt for Python source repository: https://code.qt.io/pyside/pyside-setup.git/",
        "Qt source repositories: https://code.qt.io/cgit/qt/",
        "Qt licensing: https://www.qt.io/licensing/open-source-lgpl-obligations", "",
        "Packages included:",
    ]
    for record in records:
        lines.append(f"- {record['name']} {record['version']}: https://pypi.org/project/{record['name']}/{record['version']}/")
        for title, url in sorted(record["project_urls"].items()):
            if any(word in title.lower() for word in ("source", "repository", "homepage")):
                lines.append(f"  {title}: {url}")
    (bundle / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def app_version(project: Path) -> str:
    text = (project / "pc_subtitle_translator" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+)["\']', text)
    if not match:
        raise ValueError("Could not identify the app version")
    return match.group(1)


def assemble(project: Path, cache: Path, output: Path, runtime_zip: Path, records: list[dict]) -> Path:
    if not (project / "pc_subtitle_translator" / "__main__.py").exists():
        raise FileNotFoundError("App entry point __main__.py is missing; use --download-only until source is complete")
    output.mkdir(parents=True, exist_ok=True)
    final_directory = output / BUILD_NAME
    if final_directory.exists() and not (final_directory / BUILD_SENTINEL).is_file():
        raise ValueError(f"Refusing to overwrite a directory not created by this builder: {final_directory}")
    stage = Path(tempfile.mkdtemp(prefix=".subtitle-stage-", dir=output))
    bundle = stage / BUILD_NAME
    bundle.mkdir()
    try:
        (bundle / BUILD_SENTINEL).write_text("Generated by scripts/build_portable.py\n", encoding="utf-8")
        runtime = bundle / RUNTIME_DIR
        sites = runtime / PACKAGES_DIR
        sites.mkdir(parents=True)
        extract_archive(runtime_zip, runtime)
        for record in records:
            extract_archive(cache / "wheels" / record["filename"], sites, wheel=True, runtime=runtime)
        omitted = omit_unused_qt_objects(sites)
        # The _pth file keeps the embedded interpreter independent of a system
        # Python or PYTHONPATH while enabling .pth handling needed by packages.
        (runtime / "python314._pth").write_text(
            "python314.zip\n.\np\n../app\nimport site\n", encoding="utf-8"
        )
        copy_project(project, bundle / "app")
        write_launchers(bundle)
        preserve_licenses(bundle, records)
        (bundle / "READ_ME_FIRST.txt").write_text(
            "PC subtitle translator for Windows x64\n\n"
            "1. Extract the entire Subtitles.zip into a short folder, such as your Desktop.\n"
            "   Do not run from inside the ZIP or reuse a partially extracted older copy.\n"
            "2. Open Sub and double-click Start.bat. Python and dependencies are included.\n"
            "3. Enter your OpenAI API key in the app; choose the PC playback device, source\n"
            "   language (or automatic), and target language; then start translation.\n"
            "4. Drag the subtitles into position. Play/pause is at their far right.\n\n"
            "This 0.1.1 package uses short internal paths to fix Windows extraction errors.\n"
            "No long-path registry change is needed when extracted to a normal short folder.\n\n"
            "Use Debug.bat if the app does not open. Full instructions and source are in app/.\n"
            "Internet access and a funded OpenAI API account are required for translation.\n"
            "No administrator installation is performed. All components run from this folder.\n"
            "The bundle was assembled from verified official Windows binaries. Native Windows\n"
            "audio capture and live API calls still require validation on a Windows PC.\n",
            encoding="utf-8",
        )
        manifest = {
            "app_version": app_version(project), "platform": "Windows x64",
            "python_version": PYTHON_VERSION,
            "python_archive": {"filename": PYTHON_ZIP, "url": PYTHON_URL, "sha256": PYTHON_SHA256},
            "packages": records,
            "layout": {"root": BUILD_NAME, "runtime": RUNTIME_DIR, "packages": f"{RUNTIME_DIR}/{PACKAGES_DIR}"},
            "omitted_static_build_objects": omitted,
            "windows_path_validation": audit_windows_paths(bundle),
            "validation": {"archive_hashes_verified": True, "native_windows_execution_tested": False},
            "files": [
                {"path": path.relative_to(bundle).as_posix(), "size_bytes": path.stat().st_size, "sha256": sha256(path)}
                for path in sorted(bundle.rglob("*")) if path.is_file()
            ],
        }
        (bundle / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        audit_windows_paths(bundle)
        if final_directory.exists():
            shutil.rmtree(final_directory)
        shutil.move(str(bundle), final_directory)
    finally:
        shutil.rmtree(stage)
    archive_path = output / "Subtitles.zip"
    temporary_zip = archive_path.with_suffix(".zip.part")
    with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for path in sorted(final_directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    with zipfile.ZipFile(temporary_zip) as archive:
        if archive.testzip() is not None:
            raise ValueError("Final ZIP integrity check failed")
    temporary_zip.replace(archive_path)
    archive_path.with_suffix(".zip.sha256").write_text(f"{sha256(archive_path)}  {archive_path.name}\n", encoding="ascii")
    return archive_path


def main() -> int:
    project_default = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-dir", type=Path, default=project_default)
    parser.add_argument("--cache-dir", type=Path, default=project_default.parent / "portable-assets")
    parser.add_argument("--output-dir", type=Path, default=project_default.parent / "portable-build")
    parser.add_argument("--download-only", action="store_true", help="Cache and verify dependencies without copying app sources")
    parser.add_argument("--offline", action="store_true", help="Use only already cached, verified runtime/wheels/PyPI metadata")
    args = parser.parse_args()
    project, cache, output = (path.resolve() for path in (args.project_dir, args.cache_dir, args.output_dir))
    if cache == project or project in cache.parents or output == project or project in output.parents:
        raise ValueError("Keep --cache-dir and --output-dir outside the source project")
    if cache == output or cache in output.parents or output in cache.parents:
        raise ValueError("Keep the cache and output directories separate")
    cache.mkdir(parents=True, exist_ok=True)
    runtime_zip = ensure_runtime(cache, args.offline)
    records = ensure_wheels(project, cache, args.offline)
    print(f"Verified CPython and {len(records)} pinned dependency wheels.", flush=True)
    if args.download_only:
        print(json.dumps({"cache_dir": str(cache), "download_bytes": runtime_zip.stat().st_size + sum(row["size_bytes"] for row in records)}, indent=2))
        return 0
    result = assemble(project, cache, output, runtime_zip, records)
    print(json.dumps({"archive": str(result), "size_bytes": result.stat().st_size, "sha256": sha256(result), "native_windows_tested": False}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print(f"Portable build failed: {error}", file=sys.stderr)
        raise SystemExit(1)

"""Portable Windows packaging regressions; no GUI, network, or Windows execution."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest


_BUILDER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_portable.py"
_SPEC = importlib.util.spec_from_file_location("subtitle_portable_builder_test", _BUILDER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)


def put(path: Path, content: bytes = b"fixture") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_compact_paths_leave_room_for_windows_extraction_destination(tmp_path):
    bundle = tmp_path / "Sub"
    # This is the longest retained asset from the original pinned Qt wheel.
    relative = (
        "r/p/PySide6/qml/QtQuick/Controls/FluentWinUI3/light/images/"
        "pageindicatordelegate-indicator-delegate-current-hovered@2x.png"
    )
    put(bundle / relative)
    report = builder.audit_windows_paths(bundle)
    assert report["longest_path"] == f"Sub/{relative}"
    assert report["maximum_relative_path_utf16_units"] == 126
    assert report["relative_path_budget"] == 128
    assert report["maximum_full_path_with_120_character_destination"] == 247
    assert report["native_windows_extraction_tested"] is False


def test_original_deep_qt_object_path_fails_the_packaging_gate(tmp_path):
    bundle = tmp_path / "PcSubtitleTranslator"
    put(bundle / (
        "runtime/Lib/site-packages/PySide6/qml/Qt/labs/assetdownloader/"
        "objects-RelWithDebInfo/QmlAssetDownloaderPrivate_resources_1/.qt/rcc/"
        "qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj"
    ))
    with pytest.raises(ValueError, match="budget"):
        builder.audit_windows_paths(bundle)


def test_directory_paths_are_checked_even_when_empty(tmp_path):
    bundle = tmp_path / "Sub"
    (bundle / ("a" * 125)).mkdir(parents=True)
    with pytest.raises(ValueError, match="budget"):
        builder.audit_windows_paths(bundle)


def test_windows_path_budget_counts_utf16_units(tmp_path):
    bundle = tmp_path / "Sub"
    # Non-BMP characters use two UTF-16 units; Python's len(str) alone is unsafe.
    filename = "\U0001f680" * 60 + ".txt"
    path = put(bundle / filename)
    report = builder.audit_windows_paths(bundle)
    assert report["maximum_relative_path_utf16_units"] == 128
    path.unlink()
    put(bundle / ("\U0001f680" * 61 + ".txt"))
    with pytest.raises(ValueError, match="budget"):
        builder.audit_windows_paths(bundle)


@pytest.mark.skipif(sys.platform == "win32", reason="Collision fixture requires a case-sensitive filesystem")
def test_case_insensitive_file_collision_is_rejected(tmp_path):
    bundle = tmp_path / "Sub"
    put(bundle / "Start.bat")
    put(bundle / "start.bat")
    with pytest.raises(ValueError, match="collision"):
        builder.audit_windows_paths(bundle)


@pytest.mark.skipif(sys.platform == "win32", reason="Invalid Windows names can only be created on this test host")
@pytest.mark.parametrize("name", ["CON.txt", "com1.log", "trailing.", "trailing ", 'bad:name.txt'])
def test_invalid_windows_component_is_rejected(tmp_path, name):
    bundle = tmp_path / "Sub"
    put(bundle / name)
    with pytest.raises(ValueError, match="Invalid Windows path component"):
        builder.audit_windows_paths(bundle)


def test_only_four_identified_static_objects_are_omitted(tmp_path):
    sites = tmp_path / "Sub" / "r" / "p"
    base = sites / "PySide6" / "qml" / "Qt" / "labs" / "assetdownloader"
    candidates = []
    for flavor in ("Debug", "RelWithDebInfo"):
        candidates.extend([
            base / f"objects-{flavor}" / "QmlAssetDownloaderPrivate_resources_1" / ".qt" / "rcc" / "qrc_qmake_Qt_labs_assetdownloader_init.cpp.obj",
            base / f"objects-{flavor}" / "QmlAssetDownloaderPrivateplugin_init" / "QmlAssetDownloaderPrivateplugin_init.cpp.obj",
        ])
    for index, path in enumerate(candidates):
        put(path, f"object-{index}".encode())
    preserved = [
        put(base / "assetdownloaderplugin.dll", b"runtime plugin"),
        put(base / "qmldir", b"QML metadata"),
        put(base / "objects-Debug" / "unrelated.cpp.obj", b"different object"),
        put(base / "objects-Debug" / "QmlAssetDownloaderPrivateplugin_init" / "LICENSE.txt", b"license"),
        put(sites / "PySide6" / "plugins" / "platforms" / "qwindows.dll", b"platform plugin"),
    ]
    original_bytes = {path: path.read_bytes() for path in preserved}
    result = builder.omit_unused_qt_objects(sites)
    assert {row["path"] for row in result} == {path.relative_to(sites).as_posix() for path in candidates}
    assert len(result) == 4
    for index, path in enumerate(candidates):
        assert not path.exists()
        row = next(row for row in result if row["path"] == path.relative_to(sites).as_posix())
        expected = f"object-{index}".encode()
        assert row["size_bytes"] == len(expected)
        assert row["sha256"] == hashlib.sha256(expected).hexdigest()
    assert {path: path.read_bytes() for path in preserved} == original_bytes
    # Empty deep build directories must disappear as well as their files.
    assert not (base / "objects-RelWithDebInfo").exists()
    assert builder.omit_unused_qt_objects(sites) == []


def test_launchers_reference_the_relocated_embedded_runtime(tmp_path):
    builder.write_launchers(tmp_path)
    start = (tmp_path / "Start.bat").read_bytes()
    debug = (tmp_path / "Debug.bat").read_bytes()
    assert b'"%~dp0r\\pythonw.exe" -B -m pc_subtitle_translator' in start
    assert b'"%~dp0r\\python.exe" -B -m pc_subtitle_translator' in debug
    for contents in (start, debug):
        assert b'runtime\\' not in contents
        assert b'cd /d "%~dp0"' in contents
        assert b"\r\n" in contents


def test_offline_assembly_preserves_import_locations_and_licenses(tmp_path):
    project = tmp_path / "source"
    put(project / "pc_subtitle_translator" / "__init__.py", b'__version__ = "0.1.1"\n')
    put(project / "pc_subtitle_translator" / "__main__.py", b"def main():\n    return 0\n")
    put(project / "README.md", b"Fixture app")
    runtime_zip = tmp_path / "python-runtime.zip"
    with zipfile.ZipFile(runtime_zip, "w") as archive:
        for filename in ("python.exe", "pythonw.exe", "python314.zip", "python314._pth"):
            archive.writestr(filename, b"test runtime placeholder")
        archive.writestr("LICENSE.txt", b"Python license fixture")
    cache = tmp_path / "cache"
    wheel_path = cache / "wheels" / "example_dep-1.0-py3-none-any.whl"
    wheel_path.parent.mkdir(parents=True)
    with zipfile.ZipFile(wheel_path, "w") as archive:
        archive.writestr("example_dep/__init__.py", b"VALUE = 1\n")
        archive.writestr("example_dep-1.0.dist-info/LICENSE.txt", b"Dependency license fixture")
        # Paths model the native packages whose directory relationships must survive.
        archive.writestr("numpy/__init__.py", b"")
        archive.writestr("numpy.libs/example.dll", b"native fixture")
        archive.writestr("PySide6/__init__.py", b"")
        archive.writestr("shiboken6/__init__.py", b"")
        archive.writestr("_portaudiowpatch.cp314-win_amd64.pyd", b"native fixture")
        archive.writestr("soxr/soxr_ext.pyd", b"native fixture")
    records = [{
        "name": "example-dep", "version": "1.0", "filename": wheel_path.name,
        "project_urls": {}, "sha256": hashlib.sha256(wheel_path.read_bytes()).hexdigest(),
    }]
    output = tmp_path / "output"
    result = builder.assemble(project, cache, output, runtime_zip, records)
    assert result.name == "Subtitles.zip"
    bundle = output / "Sub"
    runtime = bundle / "r"
    pth_lines = (runtime / "python314._pth").read_text(encoding="utf-8").splitlines()
    assert pth_lines == ["python314.zip", ".", "p", "../app", "import site"]
    import_locations = [(runtime / line).resolve() for line in pth_lines if line != "import site"]
    assert all(path.exists() for path in import_locations)
    search_locations = [str(path) for path in import_locations if path.is_dir()]
    dependency = importlib.machinery.PathFinder.find_spec("example_dep", search_locations)
    application = importlib.machinery.PathFinder.find_spec("pc_subtitle_translator", search_locations)
    assert dependency is not None and dependency.origin == str(runtime / "p" / "example_dep" / "__init__.py")
    assert application is not None and application.origin == str(bundle / "app" / "pc_subtitle_translator" / "__init__.py")
    for relative in ("numpy/__init__.py", "numpy.libs/example.dll", "PySide6/__init__.py", "shiboken6/__init__.py", "_portaudiowpatch.cp314-win_amd64.pyd", "soxr/soxr_ext.pyd"):
        assert (runtime / "p" / relative).is_file()
    assert (bundle / "licenses" / "Python-LICENSE.txt").read_bytes() == b"Python license fixture"
    assert (bundle / "licenses" / "example_dep-1.0.dist-info" / "LICENSE.txt").read_bytes() == b"Dependency license fixture"
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["layout"] == {"root": "Sub", "runtime": "r", "packages": "r/p"}
    assert manifest["windows_path_validation"]["maximum_relative_path_utf16_units"] <= 128
    assert manifest["validation"]["native_windows_execution_tested"] is False
    with zipfile.ZipFile(result) as archive:
        assert archive.testzip() is None
        assert "Sub/Start.bat" in archive.namelist()
        assert "Sub/r/p/example_dep/__init__.py" in archive.namelist()
        assert all(len(name.encode("utf-16-le")) // 2 <= 128 for name in archive.namelist())

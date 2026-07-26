from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
QT_VERSION = "6.11.1"
QTBASE_ARCHIVE_URL = (
    "https://download.qt.io/official_releases/qt/6.11/6.11.1/"
    "submodules/qtbase-everywhere-src-6.11.1.tar.xz"
)
QTBASE_ARCHIVE_SHA256 = (
    "d9594a31228aa23ad6b531719a29b45f0f3989fe6c136d45767ea179f233c1ac"
)
QTTRANSLATIONS_ARCHIVE_URL = (
    "https://download.qt.io/official_releases/qt/6.11/6.11.1/"
    "submodules/qttranslations-everywhere-src-6.11.1.tar.xz"
)
QTTRANSLATIONS_ARCHIVE_SHA256 = (
    "37c02c81206594c7bb4edca85ac93e8e55a9836b70c960fde6cb0f8623ec5677"
)
PYSIDE_ARCHIVE_URL = (
    "https://download.qt.io/official_releases/QtForPython/pyside6/"
    "PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz"
)
PYSIDE_ARCHIVE_SHA256 = (
    "6ffd9835bb0dd2c56f061d62f1616bb1707cfc0202b80e3165d6be087f3965e2"
)
RUNTIME_QDOC_MODULES = {"qtcore", "qtgui", "qtnetwork"}
SPDX_CREATED = "2026-05-12T00:00:00Z"
LLVMPipe_ATTRIBUTION_URL = (
    "https://doc.qt.io/qtcreator/qtcreator-binary-attribution-llvmpipe.html"
)
LLVMPipe_BINARY_SHA256 = (
    "4a7d90f91fdecb5df7b426bc2d05974b8d7ffa450af2d1f93f3eca05800718da"
)
LLVMPipe_VERSION = "Mesa 11.2.2 / LLVM 3.6.2"
LLVMPipe_COPYRIGHT = (
    "Copyright (C) 2003-2019 LLVM Team\n"
    "Copyright (C) 1999-2007 Brian Paul. All Rights Reserved."
)
MIT_LICENSE_TEXT = """Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including without
limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom
the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL BRIAN
PAUL BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."""


def _module_names(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, list):
        return {
            item.casefold()
            for item in value
            if isinstance(item, str)
        }
    return set()


def _copyright_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(item for item in value if isinstance(item, str))
    return "NOASSERTION"


def _license_file(
    attribution_path: Path,
    source_root: Path,
    entry: dict[str, object],
) -> Path:
    license_name = entry.get("LicenseFile")
    if not isinstance(license_name, str):
        license_name = ""
    relative_component = entry.get("Path", "")
    if not isinstance(relative_component, str):
        relative_component = ""
    candidates: list[Path] = []
    if license_name:
        candidates.extend(
            (
                attribution_path.parent / relative_component / license_name,
                attribution_path.parent / license_name,
                source_root / license_name,
            )
        )
    license_id = entry.get("LicenseId")
    if isinstance(license_id, str):
        for identifier in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]*", license_id):
            candidates.append(source_root / "LICENSES" / f"{identifier}.txt")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"{attribution_path}: could not resolve license file "
        f"{license_name or license_id}"
    )


def _selected_entries(
    source_root: Path,
) -> Iterable[tuple[Path, dict[str, object], Path]]:
    for attribution_path in sorted(source_root.rglob("qt_attribution.json")):
        try:
            # Qt's attribution metadata has a few historic multiline copyright
            # strings containing literal newlines. Qt's own scanner accepts
            # them, so mirror that leniency while keeping all other JSON checks.
            payload = json.loads(
                attribution_path.read_text(encoding="utf-8"),
                strict=False,
            )
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid attribution JSON: {attribution_path}: {exc}") from exc
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not (_module_names(entry.get("QDocModule")) & RUNTIME_QDOC_MODULES):
                continue
            yield (
                attribution_path,
                entry,
                _license_file(attribution_path, source_root, entry),
            )


def _spdx_id(component_id: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9.-]+", "-", component_id).strip("-.")
    if not base:
        base = "component"
    candidate = f"SPDXRef-Package-{base}"
    suffix = 2
    while candidate in used:
        candidate = f"SPDXRef-Package-{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_notices(
    source_root: Path,
    notice_output: Path,
    sbom_output: Path,
    source_output: Path,
) -> tuple[int, str]:
    source_root = source_root.resolve()
    if not (source_root / "LICENSES").is_dir():
        raise FileNotFoundError(f"not a QtBase source root: {source_root}")

    entries = list(_selected_entries(source_root))
    if not entries:
        raise ValueError("no Qt runtime attribution entries were selected")

    notice_lines = [
        f"MoonShell Qt {QT_VERSION} third-party notice inventory",
        "=" * 62,
        "",
        (
            "This file is generated from the official QtBase source attribution "
            "metadata for Qt Core, Qt GUI, and Qt Network, plus Qt's official "
            "binary attribution for the Windows software OpenGL fallback."
        ),
        (
            "It intentionally includes the complete module-level inventory. "
            "Some optional or non-Windows entries may not be compiled into the "
            "official PySide6 wheel used by a particular build."
        ),
        f"Source: {QTBASE_ARCHIVE_URL}",
        f"Source SHA-256: {QTBASE_ARCHIVE_SHA256}",
        "Generator: tools/build_qt_notices.py",
        "",
    ]

    packages: list[dict[str, object]] = []
    relationships: list[dict[str, str]] = []
    extracted_licenses: dict[str, str] = {}
    used_ids: set[str] = set()
    for attribution_path, entry, license_path in entries:
        name = str(entry.get("Name") or entry.get("Id") or "Unnamed component")
        component_id = str(entry.get("Id") or name)
        version = str(entry.get("Version") or "NOASSERTION")
        license_name = str(entry.get("License") or "NOASSERTION")
        license_id = str(entry.get("LicenseId") or "NOASSERTION")
        copyright_text = _copyright_text(entry.get("Copyright"))
        homepage = str(entry.get("Homepage") or "")
        download = str(entry.get("DownloadLocation") or "NOASSERTION")
        usage = str(entry.get("QtUsage") or "")
        relative_attribution = attribution_path.relative_to(source_root).as_posix()
        relative_license = license_path.relative_to(source_root).as_posix()
        license_text = license_path.read_text(encoding="utf-8", errors="replace").strip()

        notice_lines.extend(
            [
                "-" * 78,
                f"Component: {name}",
                f"Version: {version}",
                f"License: {license_name}",
                f"SPDX expression: {license_id}",
                f"Homepage: {homepage or 'not specified'}",
                f"Qt usage: {usage or 'not specified'}",
                f"Attribution source: {relative_attribution}",
                f"License source: {relative_license}",
                "Copyright:",
                copyright_text,
                "",
                "License text:",
                license_text,
                "",
            ]
        )

        package_id = _spdx_id(component_id, used_ids)
        package: dict[str, object] = {
            "name": name,
            "SPDXID": package_id,
            "versionInfo": version,
            "downloadLocation": download,
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": license_id,
            "copyrightText": copyright_text,
            "comment": (
                f"{usage} Attribution metadata: {relative_attribution}. "
                "This is a conservative Qt module inventory."
            ).strip(),
        }
        if homepage:
            package["homepage"] = homepage
        packages.append(package)
        relationships.append(
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": package_id,
            }
        )

        for license_ref in set(re.findall(r"LicenseRef-[A-Za-z0-9.-]+", license_id)):
            standard_text = source_root / "LICENSES" / f"{license_ref}.txt"
            extracted_licenses.setdefault(
                license_ref,
                (
                    standard_text.read_text(encoding="utf-8", errors="replace")
                    if standard_text.is_file()
                    else license_text
                ).strip(),
            )

    llvmpipe_id = _spdx_id("mesa-llvmpipe-opengl32sw", used_ids)
    notice_lines.extend(
        [
            "-" * 78,
            "Component: Mesa llvmpipe software OpenGL renderer",
            f"Version: {LLVMPipe_VERSION}",
            "License: MIT License",
            "SPDX expression: MIT",
            "Homepage: https://www.mesa3d.org/",
            "Qt usage: Windows software OpenGL fallback (opengl32sw.dll).",
            f"Attribution source: {LLVMPipe_ATTRIBUTION_URL}",
            f"License source: {LLVMPipe_ATTRIBUTION_URL}",
            f"Packaged binary SHA-256: {LLVMPipe_BINARY_SHA256}",
            "Copyright:",
            LLVMPipe_COPYRIGHT,
            "",
            "License text:",
            MIT_LICENSE_TEXT,
            "",
        ]
    )
    packages.append(
        {
            "name": "Mesa llvmpipe software OpenGL renderer",
            "SPDXID": llvmpipe_id,
            "versionInfo": LLVMPipe_VERSION,
            "downloadLocation": LLVMPipe_ATTRIBUTION_URL,
            "filesAnalyzed": False,
            "checksums": [
                {
                    "algorithm": "SHA256",
                    "checksumValue": LLVMPipe_BINARY_SHA256,
                }
            ],
            "licenseConcluded": "MIT",
            "licenseDeclared": "MIT",
            "copyrightText": LLVMPipe_COPYRIGHT,
            "homepage": "https://www.mesa3d.org/",
            "comment": (
                "Qt Windows software OpenGL fallback shipped as "
                "opengl32sw.dll. Version strings and checksum correspond to "
                "the PySide6-Essentials 6.11.1 Windows x64 wheel."
            ),
        }
    )
    relationships.append(
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": llvmpipe_id,
        }
    )

    notice_text = "\n".join(notice_lines).rstrip() + "\n"
    notice_sha256 = hashlib.sha256(notice_text.encode("utf-8")).hexdigest()
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"MoonShell-Qt-{QT_VERSION}-runtime-notice-inventory",
        "documentNamespace": (
            "https://github.com/Master-Norna/Moonshell/sbom/"
            f"qt-runtime-{QT_VERSION}-{notice_sha256[:16]}"
        ),
        "creationInfo": {
            "created": SPDX_CREATED,
            "creators": ["Tool: MoonShell-tools-build_qt_notices.py"],
            "licenseListVersion": "3.27",
        },
        "documentDescribes": [package["SPDXID"] for package in packages],
        "packages": packages,
        "relationships": relationships,
        "hasExtractedLicensingInfos": [
            {
                "licenseId": license_id,
                "extractedText": text,
                "name": license_id,
            }
            for license_id, text in sorted(extracted_licenses.items())
        ],
        "comment": (
            "Conservative attribution inventory for the QtBase modules and "
            "the Qt-distributed Windows software OpenGL fallback shipped by "
            "MoonShell. Optional/platform-specific QtBase entries may not be "
            "present in every PySide6 binary build."
        ),
    }
    component_count = len(packages)
    source_text = (
        f"QtBase source release: {QT_VERSION}\n"
        f"URL: {QTBASE_ARCHIVE_URL}\n"
        f"SHA-256: {QTBASE_ARCHIVE_SHA256}\n"
        f"Qt Translations source URL: {QTTRANSLATIONS_ARCHIVE_URL}\n"
        f"Qt Translations SHA-256: {QTTRANSLATIONS_ARCHIVE_SHA256}\n"
        f"Qt for Python / Shiboken source URL: {PYSIDE_ARCHIVE_URL}\n"
        f"Qt for Python / Shiboken SHA-256: {PYSIDE_ARCHIVE_SHA256}\n"
        f"Selected QDoc modules: {', '.join(sorted(RUNTIME_QDOC_MODULES))}\n"
        f"QtBase metadata entries: {len(entries)}\n"
        "Packaged binary extras: 1 (Mesa llvmpipe / opengl32sw.dll)\n"
        f"LLVMPipe attribution: {LLVMPipe_ATTRIBUTION_URL}\n"
        f"LLVMPipe binary SHA-256: {LLVMPipe_BINARY_SHA256}\n"
        f"Generated component entries: {component_count}\n"
        f"NOTICE SHA-256: {notice_sha256}\n"
    )

    _atomic_write(notice_output, notice_text)
    _atomic_write(
        sbom_output,
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
    )
    _atomic_write(source_output, source_text)
    return component_count, notice_sha256


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate offline Qt runtime notices and an SPDX inventory."
    )
    parser.add_argument("qtbase_source", type=Path)
    parser.add_argument(
        "--notice-output",
        type=Path,
        default=ROOT / "LICENSES" / f"Qt-{QT_VERSION}-Third-Party-Notices.txt",
    )
    parser.add_argument(
        "--sbom-output",
        type=Path,
        default=ROOT / "LICENSES" / f"Qt-{QT_VERSION}-SBOM.spdx.json",
    )
    parser.add_argument(
        "--source-output",
        type=Path,
        default=ROOT / "LICENSES" / f"Qt-{QT_VERSION}-SOURCE.txt",
    )
    args = parser.parse_args()
    count, digest = build_notices(
        args.qtbase_source,
        args.notice_output,
        args.sbom_output,
        args.source_output,
    )
    print(f"wrote Qt notices for {count} components ({digest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

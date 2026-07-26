# MoonShell third-party notices

MoonShell itself is distributed under the MIT License in `LICENSE`.
The portable Windows build also contains the following open-source
components. Exact versions used for a build are recorded in
`BUILD_INFO.json` beside `MoonShell.exe`.

## Runtime components

| Component | License | Project |
| --- | --- | --- |
| CPython (exact version recorded per build) | Python Software Foundation License 2.0 and bundled notices | https://www.python.org/ |
| Qt 6 / PySide6-Essentials / Shiboken6 | LGPL-3.0-only (also offered upstream under GPL/commercial terms) | https://doc.qt.io/qtforpython-6/ |
| Mesa llvmpipe software OpenGL fallback | MIT | https://doc.qt.io/qtcreator/qtcreator-binary-attribution-llvmpipe.html |
| OpenSSL 3 (bundled by CPython) | Apache-2.0 | https://www.openssl-library.org/source/ |
| psutil | BSD-3-Clause | https://github.com/giampaolo/psutil |
| PyInstaller bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception | https://pyinstaller.org/ |

The build uses the unmodified shared Qt libraries supplied by the official
PySide6-Essentials wheel. They are stored under MoonShell's `_internal`
directory and may be replaced with an interface-compatible build. MoonShell's
complete application source and reproducible build specification are available
at https://github.com/Master-Norna/Moonshell.

The release includes offline copies of the applicable license documents in
the `LICENSES` directory. It also includes
`Qt-6.11.1-Third-Party-Notices.txt`, a conservative module-level attribution
inventory generated from QtBase's official metadata, its source/hash record,
and `Qt-6.11.1-SBOM.spdx.json`. The inventory also records the
`opengl32sw.dll` Mesa llvmpipe fallback shipped by Qt on Windows. Some optional
or non-Windows QtBase entries in that inventory may not be compiled into the
official PySide6 wheel used by a given build. Exact QtBase, Qt Translations,
Qt for Python, and Shiboken source archive URLs and SHA-256 values are recorded
in `Qt-6.11.1-SOURCE.txt`.

CPython's Windows runtime includes OpenSSL. Its Apache-2.0 license is reproduced
in `Apache-2.0.txt`; the exact 3.0.21 source release URL and checksum are
recorded in `OpenSSL-3.0.21-SOURCE.txt`.

## Build and development components

Pillow (MIT-CMU) is used to validate sprites and generate the Windows icon but
is not imported by the frozen application. PyInstaller and
pyinstaller-hooks-contrib are build tools; only the PyInstaller bootloader is
part of the resulting application.

Qt itself contains additional third-party components. Their notices and source
references are maintained by Qt at
https://doc.qt.io/qt-6/licenses-used-in-qt.html.

No upstream project endorses MoonShell.

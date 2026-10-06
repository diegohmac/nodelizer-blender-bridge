# SPDX-License-Identifier: GPL-3.0-or-later
"""Builds the extension zip Blender installs, the same bytes every time for the same sources.

    python3 scripts/build.py            -> dist/nodelizer_bridge-<version>.zip and its .sha256

The zip holds the add-on's Python sources, its manifest and the license, so the file people
download is also its complete source.
"""

import hashlib
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, "addon")
# Zip entries carry no real times, so a rebuild gives the same checksum.
STAMP = (1980, 1, 1, 0, 0, 0)


def version() -> str:
    with open(os.path.join(ADDON, "blender_manifest.toml"), encoding="utf-8") as handle:
        match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.M)
    if not match:
        sys.exit("no version in blender_manifest.toml")
    return match.group(1)


def files():
    for name in sorted(os.listdir(ADDON)):
        path = os.path.join(ADDON, name)
        if os.path.isfile(path) and (name.endswith(".py") or name == "blender_manifest.toml"):
            yield name, path
    yield "LICENSE", os.path.join(ROOT, "LICENSE")


def main():
    out = os.path.join(ROOT, "dist")
    os.makedirs(out, exist_ok=True)
    target = os.path.join(out, f"nodelizer_bridge-{version()}.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files():
            info = zipfile.ZipInfo(name, STAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read())
    with open(target, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    with open(f"{target}.sha256", "w", encoding="utf-8") as handle:
        handle.write(f"{digest}  {os.path.basename(target)}\n")
    print(f"{os.path.relpath(target, ROOT)} {digest}")


if __name__ == "__main__":
    main()

"""Synchronize packaging versions; optionally write the release update manifest."""
import hashlib
import json
import re
import sys
from pathlib import Path

from apexity_bridge import __version__ as version

if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("Use a stable X.Y.Z version in apexity_bridge/__init__.py")
if len(sys.argv) > 1 and sys.argv[1] != f"v{version}":
    raise SystemExit("Release tag must match apexity_bridge.__version__")

for name, pattern, replacement in [
    ("packaging/installer.iss", r'#define MyAppVersion\s+"[^"]+"', f'#define MyAppVersion   "{version}"'),
    ("pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"'),
    ("packaging/file_version_info.txt", r"u'(?:1\.0\.0|\d+\.\d+\.\d+)'", f"u'{version}'"),
]:
    path = Path(name)
    path.write_text(re.sub(pattern, replacement, path.read_text(), flags=re.M))
p = Path("packaging/file_version_info.txt")
s = p.read_text()
s = re.sub(r"(filevers|prodvers)=\([^)]*\)", lambda m: f"{m[1]}=({version.replace('.', ', ')}, 0)", s)
p.write_text(s)
installer = Path(f"dist/ApexityEduBridge-Setup-{version}.exe")
if installer.exists():
    with installer.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    manifest = dict(version=version, size=installer.stat().st_size,
                    sha256=digest,
                    url=f"https://github.com/dmoshiur/apexityedu-time-1.0/releases/download/v{version}/{installer.name}")
    Path("dist/update.json").write_text(json.dumps(manifest, indent=2))

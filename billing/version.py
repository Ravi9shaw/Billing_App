"""A running host keeps its startup fingerprint even after files on disk change."""

import hashlib
import sys
from functools import lru_cache
from pathlib import Path

API_VERSION = 3
CAPABILITIES = [
    "reports",
    "customer_profiles",
    "connection",
    "configuration",
    "restart",
]


@lru_cache(maxsize=4)
def executable_digest(filename, size, modified_ns):
    digest = hashlib.sha256()
    with open(filename, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def source_build():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    paths = sorted([*root.glob("*.py"), *root.glob("web/*")])
    for path in paths:
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    if getattr(sys, "frozen", False):
        # PyInstaller stores Python code in the executable, not loose .py files.
        executable = Path(sys.executable)
        stat = executable.stat()
        digest.update(
            executable_digest(str(executable), stat.st_size, stat.st_mtime_ns)
        )
    return digest.hexdigest()[:16]


BUILD_ID = source_build()

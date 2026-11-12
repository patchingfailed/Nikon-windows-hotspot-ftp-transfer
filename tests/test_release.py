"""Release metadata checks (run without camera/network).

    python tests/test_release.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "nikon_ftp_transfer.py").read_text(encoding="utf-8")
NAMES = json.loads((ROOT / ".github" / "release-names.json").read_text(encoding="utf-8"))


def const(name):
    m = re.search(rf'^{name} = "([^"]+)"', SRC, re.M)
    assert m, f"{name} not found"
    return m.group(1)


def test_version_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", const("APP_VERSION")), const("APP_VERSION")


def test_codename_matches_release_names():
    major, minor, _ = (int(x) for x in const("APP_VERSION").split("."))
    fam = NAMES.get(str(major))
    assert fam, f"no family for major version {major}"
    assert minor < len(fam["names"]), f"no code name for {major}.{minor}"
    assert const("APP_CODENAME") == fam["names"][minor], (const("APP_CODENAME"), fam["names"][minor])


def test_release_names_unique():
    names = [n for k, f in NAMES.items() if not k.startswith("_") for n in f["names"]]
    assert len(names) == len(set(names)), "duplicate code names"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)

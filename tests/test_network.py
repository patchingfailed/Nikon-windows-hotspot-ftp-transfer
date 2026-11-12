"""Network recovery logic with a fake PowerShell layer (runs without Windows).

    python tests/test_network.py
"""
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(NFT_TEST="1", NFT_TARGET=str(Path(_tmp) / "target"), NFT_CONFIG=str(Path(_tmp) / "config.json"),
                  NFT_NO_BROWSER="1", NFT_LANG="en")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nikon_ftp_transfer as t  # noqa: E402


class FakeNet:
    """Answers 'restore' like the PowerShell script; `fail` lists profiles netsh refuses."""
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def run(self, action, timeout=60, **extra):
        self.calls.append((action, extra))
        if action == "restore":
            return {"ok": True, "iface": "Wi-Fi",
                    "restored": [{"profile": p, "ok": p not in self.fail} for p in extra["profiles"]]}
        return {"ok": True}


def last_event():
    e = t.S.events[-1]
    return e["key"], e["params"]


def test_restore_all_profiles_clears_state():
    t.NET = FakeNet()
    t.save_manual_profiles(["Hotel-Guest", "Cafe"])
    assert t.restore_profiles() is True
    assert not t.NET_STATE_FILE.exists()
    assert last_event()[0] == "ev.autoconnect_restored"


def test_partial_restore_keeps_failed_profile_and_names_adapter():
    t.NET = FakeNet(fail={"Cafe"})
    t.save_manual_profiles(["Hotel-Guest", "Cafe"])
    assert t.restore_profiles() is False
    assert t.load_manual_profiles() == ["Cafe"]
    key, params = last_event()
    assert key == "ev.autoconnect_restore_failed"
    msg = t.tr(key, "en", **params)
    assert 'name="Cafe"' in msg and 'interface="Wi-Fi"' in msg, msg
    t.NET_STATE_FILE.unlink(missing_ok=True)


def test_nothing_to_restore_does_not_call_powershell():
    t.NET = FakeNet()
    t.NET_STATE_FILE.unlink(missing_ok=True)
    assert t.restore_profiles() is True
    assert t.NET.calls == []


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

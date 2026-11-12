"""Tests for the image number logic and progress (run without camera/network).

    python -m pytest tests/          or    python tests/test_progress.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(NFT_TEST="1", NFT_TARGET=str(Path(_tmp) / "target"), NFT_CONFIG=str(Path(_tmp) / "config.json"),
                  NFT_NO_BROWSER="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nikon_ftp_transfer as t  # noqa: E402


def reset(existing=()):
    t.S.nums = {}
    t.S.sess = t.new_session()
    t.S.expect = None
    t.S.expect_events = set()
    for name in existing:
        k = t.num_key(name)
        t.S.nums.setdefault(k[0], {}).setdefault(k[1], set())


def recv(*names, status="ok"):
    for n in names:
        t.note_received(n, status)


def expect(mode, value, first=None):
    ok, msg = t.set_expectation({"mode": mode, "value": value, "first": first})
    assert ok, msg


def test_num_key():
    assert t.num_key("NZ8_6911.NEF") == ("NZ8", 6911)
    assert t.num_key("_NZ86911.NEF") == ("NZ8", 6911)
    assert t.num_key("DSC_0001.JPG") == ("DSC", 1)
    assert t.num_key("DSC_0001_2.NEF") is None
    assert t.num_key("notes.txt") is None


def test_rollover_helpers():
    assert t.nstep(9999, 1) == 1
    assert t.nstep(1, -1) == 9999
    assert t.ndist(9998, 2) == 3
    assert t.circ_max([9997, 9998, 9999, 1, 2]) == 2
    assert t.circ_max([10, 11, 12]) == 12
    assert t.parse_number("NZ8_7100.NEF") == ("NZ8", 7100)
    assert t.parse_number("7100") == (None, 7100)


def test_files_mode():
    reset()
    expect("files", "1 of 5")
    recv("DSC_0001.NEF", "DSC_0002.NEF")
    p = t.progress()
    assert (p["total"], p["done"], p["remaining"]) == (5, 2, 3)


def test_last_mode_with_rollover():
    reset([f"NZ8_{n:04d}.NEF" for n in range(9990, 9998)])        # present up to 9997
    expect("last", "NZ8_0003.NEF")                                 # today 9998..0003 = 5 images
    recv("NZ8_9998.NEF", "NZ8_9999.NEF", "NZ8_0001.NEF")
    p = t.progress()
    assert p["first"] == 9998 and p["total"] == 5 and p["done"] == 3, p
    assert p["missing"] == [2, 3]


def test_last_mode_raw_plus_jpeg_counts_once():
    reset(["DSC_0100.NEF"])
    expect("last", "103")
    recv("DSC_0101.NEF", "DSC_0101.JPG", "DSC_0102.NEF", "DSC_0102.JPG")
    p = t.progress()
    assert (p["total"], p["done"]) == (3, 2), p


def test_truncated_file_counts_as_missing():
    reset(["DSC_0100.NEF"])
    expect("last", "102")
    recv("DSC_0101.NEF")
    recv("DSC_0102.NEF", status="warn")
    p = t.progress()
    assert p["done"] == 1 and p["missing"] == [102], p
    recv("DSC_0102.NEF", status="ok")       # complete version delivered later
    assert t.progress()["done"] == 2


def test_card_mode_ascending_new_files():
    # Card: 0500..0519 (20 images), 0500..0509 already on the laptop, older card before (0400..0450)
    reset([f"DSC_{n:04d}.NEF" for n in list(range(400, 451)) + list(range(500, 510))])
    expect("card", "1 of 20")
    recv("DSC_0510.NEF", "DSC_0511.NEF", "DSC_0512.NEF")
    p = t.progress()
    assert p["first"] == 500, p
    assert (p["total"], p["done"]) == (20, 13), p


def test_card_mode_full_resend():
    reset([f"DSC_{n:04d}.NEF" for n in range(500, 510)])
    expect("card", "20")
    recv("DSC_0500.NEF", "DSC_0501.NEF")                 # camera resends everything from the start
    p = t.progress()
    assert p["first"] == 500 and p["done"] == 10, p


def test_card_mode_descending():
    reset([f"DSC_{n:04d}.NEF" for n in range(500, 510)])
    expect("card", "20")
    recv("DSC_0519.NEF", "DSC_0518.NEF", "DSC_0517.NEF")
    p = t.progress()
    assert p["direction"] == "down" and p["first"] == 500, p
    assert p["done"] == 13, p


def test_card_mode_with_deleted_images_and_manual_first():
    # 0505 deleted in the camera: card has 0500..0520 without 0505 = 20 images
    reset([f"DSC_{n:04d}.NEF" for n in range(500, 505)])
    expect("card", "20", first="DSC_0500")
    recv(*[f"DSC_{n:04d}.NEF" for n in range(506, 521)])
    p = t.progress()
    assert (p["total"], p["done"], p["remaining"]) == (20, 20, 0), p
    assert p["missing"] == [505]               # the gap is shown as a candidate


def test_stalled_and_ack():
    reset()
    expect("files", "4")
    recv("DSC_0001.NEF")
    t.S.last_activity = time.time() - 120
    p = t.progress()
    assert p["state"] == "stalled", p
    t.S.expect["ack"] = "ok"
    t.S.expect["ack_items"] = 2
    assert t.progress()["ack"] == "ok"
    recv("DSC_0002.NEF")                          # new activity cancels the acknowledgement
    assert t.progress()["ack"] is None


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

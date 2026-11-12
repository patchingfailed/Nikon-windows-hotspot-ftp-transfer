"""Tests for the translation tables (run without camera/network).

    python -m pytest tests/          or    python tests/test_i18n.py
"""
import json
import os
import re
import string
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ.update(NFT_TEST="1", NFT_TARGET=str(Path(_tmp) / "target"), NFT_CONFIG=str(Path(_tmp) / "config.json"),
                  NFT_NO_BROWSER="1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import nikon_ftp_transfer as t  # noqa: E402

DE = json.loads((ROOT / "locales" / "de.json").read_text(encoding="utf-8"))
EN = json.loads((ROOT / "locales" / "en.json").read_text(encoding="utf-8"))
PREFIXES = ("ev", "hint", "prog", "file", "verify", "ps", "msg", "ui")
KEY_RE = r"(?:%s)(?:\.[a-z0-9_]+)+" % "|".join(PREFIXES)


def placeholders(template):
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_same_keys_same_order():
    assert list(DE) == list(EN), sorted(set(DE) ^ set(EN))


def test_key_prefixes():
    for key in EN:
        assert key.split(".")[0] in PREFIXES, key


def test_same_placeholders():
    for key in EN:
        assert placeholders(DE[key]) == placeholders(EN[key]), (key, DE[key], EN[key])


def test_no_empty_values():
    for key in EN:
        assert DE[key].strip() and EN[key].strip(), key


def test_python_keys_exist():
    src = (ROOT / "nikon_ftp_transfer.py").read_text(encoding="utf-8")
    used = set(re.findall(r"""["'](%s)["']""" % KEY_RE, src))
    used.discard("ui.html")      # a file name, not a key
    assert used, "no locale keys found in nikon_ftp_transfer.py"
    assert not [k for k in used if k not in EN], sorted(k for k in used if k not in EN)


def test_progress_code_keys_exist():
    for unit in ("files", "images"):
        assert f"prog.unit.{unit}" in EN
    for code in ("after_last_existing", "first_of_session", "given", "estimated_newest_first",
                 "first_was_present", "estimated_from_existing"):
        assert f"prog.anchor.{code}" in EN, code
    for code in ("need_first_file_range", "need_first_file_card"):
        assert f"prog.waiting.{code}" in EN, code
    for code in ("range_too_big", "range_vs_card"):
        assert f"prog.note.{code}" in EN, code


def test_ui_html_keys_exist():
    ui = ROOT / "ui.html"
    if not ui.exists():
        return
    # keys from calls like t('key') / t("key") / t('key', {...})
    used = set(re.findall(r"""\bt\(\s*['"](%s)['"]""" % KEY_RE, ui.read_text(encoding="utf-8")))
    used = {k for k in used if k.split(".")[0] in PREFIXES}
    assert not [k for k in used if k not in EN], sorted(k for k in used if k not in EN)


def test_ui_html_data_i18n_keys_exist():
    ui = ROOT / "ui.html"
    if not ui.exists():
        return
    # static texts: data-i18n="key" and data-i18n-aria="key"
    used = set(re.findall(r'data-i18n(?:-aria)?="([^"]+)"', ui.read_text(encoding="utf-8")))
    assert used, "no data-i18n attributes found in ui.html"
    assert not [k for k in used if k not in EN], sorted(k for k in used if k not in EN)


def test_source_is_pure_ascii():
    data = (ROOT / "nikon_ftp_transfer.py").read_bytes()
    bad = [i for i, line in enumerate(data.splitlines(), 1) if any(b > 127 for b in line)]
    assert not bad, f"non-ASCII characters in nikon_ftp_transfer.py, lines {bad[:10]}"


def test_tr_lookup_and_fallback():
    assert t.tr("ev.ftp_disconnect", "en") == EN["ev.ftp_disconnect"]
    assert t.tr("ev.ftp_disconnect", "de") == DE["ev.ftp_disconnect"]
    assert t.tr("does.not.exist", "de") == "does.not.exist"
    assert t.tr("", "de") == ""
    assert t.tr("ev.ftp_connect", "de", ip="1.2.3.4") == DE["ev.ftp_connect"].format(ip="1.2.3.4")


def test_tr_missing_param_returns_template():
    assert t.tr("ev.ftp_connect", "en") == EN["ev.ftp_connect"]          # {ip} missing: no exception
    assert t.tr("ev.ftp_connect", "en", other=1) == EN["ev.ftp_connect"]


def test_tr_unknown_language_falls_back_to_english():
    assert t.tr("ev.ftp_disconnect", "xx") == EN["ev.ftp_disconnect"]


def test_tr_renders_nested_refs():
    text = t.tr("ev.expect_set", "de", label=t.ref("prog.mode.card"), value="20")
    assert text == DE["ev.expect_set"].format(label=DE["prog.mode.card"], value="20")


def test_verify_returns_keys():
    p = Path(_tmp) / "bad.jpg"
    p.write_bytes(b"not a jpeg at all")
    ok, key, params = t.verify(p)
    assert ok is False and key in EN, (ok, key)
    p2 = Path(_tmp) / "good.jpg"
    p2.write_bytes(b"\xff\xd8" + b"x" * 10 + b"\xff\xd9")
    assert t.verify(p2) == (True, None, {})


def test_state_is_rendered_in_current_language():
    t.event("info", "ev.session_reset")
    try:
        t.set_language("de")
        assert t.api_state()["lang"] == "de"
        assert t.api_state()["events"][0]["msg"] == DE["ev.session_reset"]
        t.set_language("en")
        assert t.api_state()["events"][0]["msg"] == EN["ev.session_reset"]
    finally:
        t.set_language("en")


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

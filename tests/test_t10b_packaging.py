"""T10b: embedded ._pth normalisation and pack upgrade safety."""

from __future__ import annotations

from tools.fix_embedded_pth import normalise_pth


def test_normalise_pth_adds_dot_and_disables_site():
    original = "python312.zip\n\n# Uncomment to run site.main() automatically\n#import site\n"
    out = normalise_pth(original)
    assert "python312.zip" in out
    lines = out.splitlines()
    assert "." in lines
    assert "#import site" in out
    assert not any(l.strip() == "import site" for l in lines)


def test_normalise_pth_is_idempotent():
    original = "python312.zip\n.\n#import site\n"
    once = normalise_pth(original)
    twice = normalise_pth(once)
    assert once == twice


def test_normalise_pth_keeps_zip():
    out = normalise_pth("python312.zip\n")
    assert out.splitlines()[0] == "python312.zip"

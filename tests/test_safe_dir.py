"""Unit tests for the loader's source-path guard.

The source directories come from argv, so the loader resolves each and refuses
one that escapes the project root. These pin that so a path-traversal argument
cannot point the loader at files outside the tree.
"""
import pytest

from nfr.load_sources import PROJECT_ROOT, _quoted_ident, _safe_dir, _safe_input_file


def test_accepts_a_path_inside_the_project():
    p = _safe_dir("data/sample")
    assert PROJECT_ROOT in p.parents or p == PROJECT_ROOT


def test_accepts_the_project_root_itself():
    assert _safe_dir(str(PROJECT_ROOT)) == PROJECT_ROOT


def test_rejects_an_absolute_path_outside_the_project():
    with pytest.raises(SystemExit):
        _safe_dir("/etc")


def test_rejects_a_traversal_escape():
    with pytest.raises(SystemExit):
        _safe_dir("../../../../tmp")


def test_quoted_ident_accepts_known_tables():
    assert _quoted_ident("grid3_v2") == '"grid3_v2"'


def test_quoted_ident_rejects_unknown_name():
    # An off-allowlist name (where injected SQL would hide) is refused.
    with pytest.raises(ValueError):
        _quoted_ident("grid3_v2; drop table users")


def test_safe_input_file_rejects_a_symlink_escaping_the_project(tmp_path, monkeypatch):
    # A file inside an allowed dir that symlinks outside the project is refused.
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n")
    inside_dir = PROJECT_ROOT / "data" / "sample"
    link = inside_dir / "_test_evil_link.csv"
    try:
        link.symlink_to(outside)
        with pytest.raises(SystemExit):
            _safe_input_file(link)
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()


def test_safe_input_file_accepts_a_real_project_file():
    real = _safe_input_file(PROJECT_ROOT / "data" / "frame" / "nigeria_master_lga_index.csv")
    assert real.is_file()

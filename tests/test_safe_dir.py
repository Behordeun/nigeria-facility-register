"""Unit tests for the loader's source-path guard.

The source directories come from argv, so the loader resolves each and refuses
one that escapes the project root. These pin that so a path-traversal argument
cannot point the loader at files outside the tree.
"""
import pytest

from nfr.load_sources import PROJECT_ROOT, _safe_dir


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

"""The atomic CSV writer the build scripts share."""
from __future__ import annotations

import os

import pandas as pd
import pytest

from nfr import files
from nfr.files import write_csv_atomic


def test_writes_a_new_file_and_replaces_an_existing_one(tmp_path):
    out = tmp_path / "derived" / "table.csv"
    write_csv_atomic(pd.DataFrame({"a": [1, 2]}), out)
    assert out.read_text() == "a\n1\n2\n", "the parent folder is created and the rows written"
    write_csv_atomic(pd.DataFrame({"a": [3]}), out)
    assert out.read_text() == "a\n3\n", "a second write replaces the file"
    assert {p.name for p in out.parent.iterdir()} == {"table.csv"}, "no temporary file is left behind"


def test_a_failed_write_leaves_the_existing_file_and_no_temp_file(tmp_path, monkeypatch):
    out = tmp_path / "table.csv"
    out.write_text("a\nold\n")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail)
    with pytest.raises(OSError, match="disk full"):
        write_csv_atomic(pd.DataFrame({"a": [1]}), out)
    assert out.read_text() == "a\nold\n", "the published table must be untouched"
    assert {p.name for p in tmp_path.iterdir()} == {"table.csv"}, "the partial temp file must be removed"


def test_a_failure_to_open_the_temp_file_closes_its_descriptor(tmp_path, monkeypatch):
    created = []
    real_mkstemp = files.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        created.append(fd)
        return fd, name

    def failing_fdopen(*args, **kwargs):
        raise OSError("cannot wrap descriptor")

    monkeypatch.setattr(files.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(files.os, "fdopen", failing_fdopen)
    with pytest.raises(OSError, match="cannot wrap descriptor"):
        write_csv_atomic(pd.DataFrame({"a": [1]}), tmp_path / "table.csv")
    (fd,) = created
    with pytest.raises(OSError):
        os.fstat(fd)  # a closed descriptor cannot be inspected
    assert not list(tmp_path.iterdir()), "the temp file must be removed too"

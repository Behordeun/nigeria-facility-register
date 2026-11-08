"""
Writing the committed derived tables safely.

The build scripts under scripts/ write per-LGA CSVs into data/derived/, which the
loader and the dbt tests read. A write that stops part-way must never leave a
truncated table behind, so each one goes to a temporary file in the same
directory and is renamed over the target in one step.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pandas as pd


def write_csv_atomic(table: pd.DataFrame, out: Path) -> None:
    """Write `table` to `out` as CSV, replacing any existing file atomically.

    The rows go to a temporary file beside `out` (".<name>.*.part"), which then
    replaces `out` with os.replace, so a reader sees either the old file or the
    whole new one. On any failure the temporary file is removed, `out` is left
    as it was, and the error propagates.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f".{out.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        try:
            fh = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            # fdopen owns the descriptor only once it succeeds; close it here
            # so a failure to wrap it does not leak it.
            os.close(fd)
            raise
        with fh:
            table.to_csv(fh, index=False)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

"""Canonical name keys for joining facilities, the frame and burden data.

LGA and state names differ across sources only in punctuation and spelling.
Every join in this project goes through these two functions so a hyphen or a
double space never splits a real match. Covered by tests/test_norm_keys.py.
"""
from __future__ import annotations

import re

_STATE_FIX = {
    "FCT, Abuja": "FCT",
    "Fct": "FCT",
    "Fct, Abuja": "FCT",
    "Federal Capital Territory": "FCT",
    "Abuja": "FCT",
    "Nassarawa": "Nasarawa",
}


def norm_state(s: str | None) -> str:
    s = (s or "").strip()
    return _STATE_FIX.get(s, s)


def norm_lga(s: str | None) -> str:
    s = (s or "").strip().lower()
    s = s.replace("/", " ").replace("-", " ").replace(".", " ")
    return re.sub(r"\s+", " ", s).strip()

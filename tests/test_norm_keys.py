"""Unit tests for the LGA/state name normalisation that drives every join.

These keys decide whether a facility lands on the right LGA, so the punctuation
and spacing variants seen in the real data are pinned here as a contract.
"""
from nfr.keys import norm_lga, norm_state


def test_hyphen_and_space_collapse_to_same_key():
    # The real split that produced 40 false zero-facility LGAs.
    assert norm_lga("Akoko-Edo") == norm_lga("Akoko Edo")
    assert norm_lga("Esan North-East") == norm_lga("Esan North East")
    assert norm_lga("Warri South-West") == norm_lga("Warri South West")


def test_slash_is_normalised():
    assert norm_lga("Urue Offong/Oruko") == norm_lga("Urue Offong Oruko")


def test_runs_of_whitespace_collapse():
    assert norm_lga("Esan  North   East") == "esan north east"


def test_state_variants_map_to_one_canonical():
    assert norm_state("FCT, Abuja") == norm_state("Fct")
    assert norm_state("Federal Capital Territory") == norm_state("FCT")
    assert norm_state("Nassarawa") == norm_state("Nasarawa")


def test_empty_is_safe():
    assert norm_lga(None) == ""
    assert norm_state("") == ""

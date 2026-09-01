"""Name matching between the odds feed and CFBD.

A mis-resolved name is worse than an unmatched one: it produces a confident
bet computed against the wrong team's rating. These tests exist to keep that
failure mode impossible rather than unlikely.
"""

from __future__ import annotations

from gridiron.sources.oddsapi import BOOK_ALIASES, TeamMatcher

TEAMS = [
    ("Alabama", "Crimson Tide"),
    ("Miami", "Hurricanes"),
    ("Miami (OH)", "RedHawks"),
    ("Ole Miss", "Rebels"),
    ("Texas A&M", "Aggies"),
    ("San José State", "Spartans"),
    ("Hawai'i", "Rainbow Warriors"),
    ("Louisiana", "Ragin' Cajuns"),
    ("Appalachian State", "Mountaineers"),
    ("North Carolina", "Tar Heels"),
    ("North Carolina State", None),
    ("NC State", "Wolfpack"),
]


def matcher() -> TeamMatcher:
    return TeamMatcher(TEAMS)


def test_school_plus_mascot_resolves_exactly():
    assert matcher().resolve("Alabama Crimson Tide") == "Alabama"


def test_the_bare_school_name_resolves():
    assert matcher().resolve("Alabama") == "Alabama"


def test_the_two_miamis_stay_apart():
    resolve = matcher().resolve
    assert resolve("Miami Hurricanes") == "Miami"
    assert resolve("Miami (OH) RedHawks") == "Miami (OH)"
    assert resolve("Miami RedHawks") == "Miami (OH)"


def test_punctuation_and_accents_do_not_matter():
    resolve = matcher().resolve
    assert resolve("San Jose State Spartans") == "San José State"
    assert resolve("Hawaii Rainbow Warriors") == "Hawai'i"
    assert resolve("Louisiana Ragin Cajuns") == "Louisiana"


def test_ampersands_survive_normalisation():
    assert matcher().resolve("Texas A&M Aggies") == "Texas A&M"


def test_an_unknown_name_returns_none_rather_than_a_guess():
    assert matcher().resolve("Springfield Atoms") is None


def test_prefix_fallback_prefers_the_longest_school_match():
    """'North Carolina State ...' must not collapse to 'North Carolina'."""
    assert matcher().resolve("North Carolina State Wolfpack") == "NC State"
    assert matcher().resolve("North Carolina Tar Heels") == "North Carolina"


def test_prefix_matching_needs_a_word_boundary():
    """'Alabamaville' is not Alabama."""
    assert matcher().resolve("Alabamaville Tigers") is None


def test_pointsbet_prices_are_reported_as_fanatics():
    """The feed carried both keys through the acquisition; one label out."""
    assert BOOK_ALIASES["pointsbetus"] == "fanatics"
    assert BOOK_ALIASES["fanatics"] == "fanatics"
    assert BOOK_ALIASES["draftkings"] == "draftkings"

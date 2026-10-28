"""Tests for Typosquat Radar.  Run with:  python3 -m pytest -q

None of these touch the network. The C++ matcher is compiled once and fed
small hand-written name lists whose correct answers are known in advance.
"""

import random
import string
import subprocess

import pytest

import radar


@pytest.fixture(scope="session")
def match(tmp_path_factory):
    """Run match.cpp on given names/targets and return its rows as dicts."""
    radar.ensure_binary()
    tmp = tmp_path_factory.mktemp("match")

    def run(names, targets):
        nf, tf = tmp / "names.txt", tmp / "targets.txt"
        nf.write_text("\n".join(names) + "\n", encoding="utf-8")
        tf.write_text("\n".join(targets) + "\n", encoding="utf-8")
        out = subprocess.run([str(radar.MATCH_BIN), str(nf), str(tf), "1"],
                             capture_output=True, text=True, encoding="utf-8", check=True).stdout
        rows = [line.split("\t") for line in out.splitlines()]
        return {r[1]: {"target": r[0], "distance": int(r[2]), "kind": r[3], "detail": r[4]} for r in rows}

    return run


# --------------------------------------------------------------------------
# C++ matcher: typo classification on known cases
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name, kind", [
    ("reqeusts", "swap"),          # e and u swapped
    ("requezts", "keyboard"),      # s -> z, neighbouring keys
    ("requestx", "keyboard"),      # s -> x
    ("reqvests", "substitution"),  # u -> v, not neighbours
    ("requestss", "double"),       # doubled letter
    ("arequests", "insertion"),    # extra letter that isn't a double
    ("requets", "omission"),       # missing s
    ("request5", "lookalike"),     # 5 reads as s
])
def test_one_edit_kinds(match, name, kind):
    rows = match([name, "requests"], ["requests"])
    assert rows[name]["kind"] == kind
    assert rows[name]["distance"] == 1


def test_separator_typos(match):
    rows = match(["pythondateutil", "python-date-util"], ["python-dateutil"])
    assert rows["pythondateutil"]["kind"] == "separator"       # dash dropped
    assert rows["python-date-util"]["kind"] == "separator"     # dash added


def test_multi_character_lookalikes(match):
    # "rn" reads as "m" and "0" as "o": edit distance 2+, but they look identical.
    rows = match(["rnatplotlib", "djang0", "f1ask"], ["matplotlib", "django", "flask"])
    assert rows["djang0"]["kind"] == "lookalike"
    assert rows["f1ask"]["kind"] == "lookalike"
    assert rows["rnatplotlib"]["kind"] in ("lookalike", "two-edits")   # long name: also 2 edits


def test_exact_and_distant_names_are_ignored(match):
    rows = match(["requests", "flask", "totally-different"], ["requests"])
    assert rows == {}              # the real package and unrelated names never appear


def test_short_names_only_allow_one_edit(match):
    # "six" -> "sxx" is one edit; "sax1" is two, which is too loose for a 3-letter name.
    rows = match(["sxx", "sax1"], ["six"])
    assert "sxx" in rows and "sax1" not in rows


def test_long_names_allow_two_edits(match):
    rows = match(["matplotlbi", "matpltolib"], ["matplotlib"])   # one swap each
    rows2 = match(["mtaplotlbi"], ["matplotlib"])                 # two swaps
    assert rows["matplotlbi"]["distance"] == 1
    assert rows2["mtaplotlbi"]["distance"] == 2


# --------------------------------------------------------------------------
# Differential test: C++ and the Python port must agree on every pair
# --------------------------------------------------------------------------

def test_cpp_matches_python_on_random_names(match):
    rng = random.Random(1234)                 # fixed seed: failures are reproducible
    alphabet = string.ascii_lowercase[:6] + "-"
    targets = ["abcdef", "abcdefabcd", "fedcba"]
    names = set()
    for t in targets:                          # random mutations of each target, plus noise
        for _ in range(400):
            s = list(t)
            for _ in range(rng.randint(1, 3)):
                op = rng.choice("isdt")
                pos = rng.randrange(len(s) + (op == "i"))
                if op == "i":
                    s.insert(pos, rng.choice(alphabet))
                elif op == "d" and len(s) > 1:
                    s.pop(min(pos, len(s) - 1))
                elif op == "s":
                    s[min(pos, len(s) - 1)] = rng.choice(alphabet)
                elif op == "t" and len(s) > 1:
                    p = min(pos, len(s) - 2)
                    s[p], s[p + 1] = s[p + 1], s[p]
            names.add("".join(s))
    names = sorted(names - set(targets))

    cpp_rows = match(names, targets)
    for t in targets:
        limit = 2 if len(t) >= 10 else 1
        expected = {n for n in names if radar.py_osa_distance(n, t, limit) <= limit}
        got = {n for n, r in cpp_rows.items() if r["target"] == t and r["distance"] > 0}
        assert got == expected, f"mismatch for {t}"


def test_python_distance_known_values():
    d = radar.py_osa_distance
    assert d("requests", "requests", 2) == 0
    assert d("reqeusts", "requests", 2) == 1      # swap counts as one edit
    assert d("rqeuests", "requests", 2) == 1
    assert d("abc", "xyz", 1) == 2               # gives up at limit + 1


# --------------------------------------------------------------------------
# Python side: normalization, requirements parsing, risk scoring
# --------------------------------------------------------------------------

def test_normalize_follows_pep503():
    assert radar.normalize("Typing_Extensions") == "typing-extensions"
    assert radar.normalize("zope.interface") == "zope-interface"
    assert radar.normalize("A__b--C") == "a-b-c"


def test_parse_requirements_strips_everything_but_names():
    text = """
    # comment line
    requests>=2.31  # trailing comment
    Django[argon2]==4.2 ; python_version >= "3.8"
    -r other.txt
    --index-url https://example.com/simple
    numpy
    typing_extensions~=4.0
    """
    assert radar.parse_requirements(text) == ["requests", "django", "numpy", "typing-extensions"]


NOW = radar.datetime(2026, 10, 1, tzinfo=radar.timezone.utc)


def meta(**kw):
    base = {"exists": True, "summary": "A real library", "has_links": True, "releases": 20,
            "first_upload": "2015-01-01T00:00:00Z", "last_upload": "2026-01-01T00:00:00Z",
            "latest_size": 250_000}
    base.update(kw)
    return base


def test_fresh_empty_package_is_high_risk():
    m = meta(summary="", has_links=False, releases=1, first_upload="2026-09-20T00:00:00Z", latest_size=3_000)
    score, reasons = radar.risk_score(m, "swap", now=NOW)
    assert radar.level(score) == "HIGH"
    assert "only 1 release" in reasons and "no description" in reasons


def test_established_project_is_low_risk_even_with_close_name():
    score, _ = radar.risk_score(meta(), "keyboard", now=NOW)
    assert radar.level(score) == "LOW"


def test_missing_project_gets_reserved_label():
    score, reasons = radar.risk_score({"exists": False}, "swap", now=NOW)
    assert radar.level(score) == "LOW" and "no releases" in reasons[0]


def test_trim_metadata_counts_only_releases_with_files():
    doc = {
        "info": {"summary": " hi ", "home_page": "", "project_urls": {"Source": "https://x"}},
        "releases": {
            "0.1": [{"upload_time_iso_8601": "2020-01-01T00:00:00Z"}],
            "0.2": [],                                                   # yanked/empty release
            "0.3": [{"upload_time_iso_8601": "2021-05-01T00:00:00Z"},
                    {"upload_time_iso_8601": "2021-05-02T00:00:00Z"}],
        },
        "urls": [{"size": 1200}, {"size": 4800}],
    }
    m = radar.trim_metadata(doc)
    assert m["releases"] == 2
    assert m["summary"] == "hi" and m["has_links"]
    assert m["first_upload"].startswith("2020") and m["last_upload"].startswith("2021-05-02")
    assert m["latest_size"] == 4800

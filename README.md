# Typosquat Radar

Typosquatting is when someone publishes a package with a name one typo away from a popular one (`pamdas` for `pandas`) and waits for people to mistype it in `pip install`. Typosquat Radar scans every name on PyPI, finds the ones that imitate popular packages, labels the trick each one uses, and ranks them by how suspicious the package itself looks.

Python handles downloading, caching, risk scoring, and output. The name matching (about 20 million comparisons per full scan) runs in C++.

## What it found (scan of October 26, 2026)

| | |
|---|---|
| Names on PyPI scanned | 923,796 |
| Popular packages checked | 107 |
| Name comparisons | 19,531,647 |
| Lookalike names found | 1,511 |
| Rated high risk | 71 |
| Lookalikes first uploaded in the last 90 days | 42 |

A few things that stood out:

- `numpyr`, `numpyt`, and `numpyu` were all uploaded on April 26, 2026, within 19 minutes of each other, each with one release and no description. That looks like one person registering a batch of typos.
- `pip` has the most lookalikes (120), mostly because a three-letter name has a lot of one-letter neighbours. Short names are noisy, which is why the matcher only allows two edits for names of 10+ characters.
- Most lookalikes are low risk: old, actively released projects that happen to have similar names (`grequests` has existed since 2012). The risk score is what separates them from the empty one-release packages.

## Demo

**Check one package**

```
$ python3 radar.py check pandas
Scanned 923,796 names in 0.02s, 38 lookalikes of pandas

  pamdas                 keyboard slip (n -> m)         registered 16 months ago 1 release    HIGH    85
  pajdas                 keyboard slip (n -> j)         registered 5 months ago  2 releases   HIGH    75
  paandas                doubled letter (a)             registered 2022          3 releases   HIGH    60
  pandes                 letter changed (a -> e)        registered 15 months ago 1 release    HIGH    60
  padnas                 letters swapped (n/d)          registered 2020          1 release    MEDIUM  55
  fandas                 letter changed (p -> f)        registered 20 months ago 1 release    MEDIUM  50
```

**Scan a requirements file**

```
$ python3 radar.py scan requirements.txt
  OK       numpy, flask, requests
  HIGH     pamdas  <- keyboard slip (n -> m) of "pandas"; first upload under 2 years ago, only 1 release, no description, no homepage or repo link, tiny package (3 KB), deceptive typo type
  MISSING  python-dateutils  <- extra letter (s) of "python-dateutil"; not on PyPI; installs fail today and an attacker could try to register it
```

It exits with status 1 when it finds anything high risk or missing, so it can run as a CI step and fail the build.

**Interactive page**

`python3 radar.py ui` opens a browser page with a radial chart per package: lookalikes on rings around it (inner ring: lookalike characters, middle: one edit, outer: two edits), colored by risk. Details are in the Interactive page section below.

## Quick start

Needs Python 3.9+ and `g++` (on Windows, MinGW-w64 g++ works; use `python` instead of `python3`). No Python packages beyond the standard library (pytest only for tests).

```
python3 radar.py fetch                 # download all PyPI names (~45 MB, a few seconds)
python3 radar.py sweep                 # scan all 107 popular packages, then open the page on the map
python3 radar.py check scikit-learn    # lookalikes of any package, then open the page on it
python3 radar.py scan requirements.txt # check your dependencies, then open the page on the results
python3 radar.py top                   # popular packages ranked by risky lookalikes, then the map
```

Each of these prints its results in the terminal and then opens the interactive page in your browser on the matching view. Add `--no-ui` to only print (for scripts and CI). Press Ctrl+C to stop the page.

Other commands:

```
python3 radar.py ui                    # open the page on its own
python3 report.py                      # save a static report.html snapshot of the sweep
python3 radar.py bench 107             # C++ vs pure Python timing
python3 -m pytest -q                   # tests
```

`match.cpp` compiles itself the first time it's needed. Downloaded data and the metadata cache go in `data/`.

## Interactive page

`python3 radar.py ui` starts a small local web server (standard library only) and opens it in your browser:

- **Sidebar:** the popular packages ranked by high and medium risk lookalikes, with a filter, plus a box to check any package name live.
- **Package view:** a radial chart of every lookalike (hover a dot for details, click it to jump to its entry), filters by risk level, and each lookalike's age, releases, description, and the reasons behind its score, linked to its PyPI page.
- **Ecosystem map:** one small chart per popular package from the last sweep; click any to open it.
- **Scan requirements:** paste a requirements.txt and get a verdict per dependency.

## How it works

1. **Fetch.** PyPI's index API lists every project name in one JSON response. Names are normalized the way PyPI does it (`Typing_Extensions` and `typing-extensions` are the same project).
2. **Match (C++).** For each popular package, `match.cpp` compares it against every name of similar length using edit distance, counting a swap of two neighbouring letters as one typo. It stops early on any comparison that's already too far off, which is most of them. Names are also reduced to a "skeleton" where lookalike characters are merged (`0` to `o`, `rn` to `m`, `1` to `l`), so `djang0` is caught by a hash lookup even though it's not a simple typo. Each match gets a label: swap, keyboard slip (QWERTY neighbours), doubled letter, missing letter, lookalike character, separator, and so on. Work is split across threads by target.
3. **Score (Python).** For each match, the tool fetches the package's metadata from the PyPI JSON API (cached, 8 requests in parallel, backing off on rate limits) and scores it from 0 to 100: recent first upload, one release, no description, no project links, a tiny file, and a deceptive typo type all add points. An old project with many releases loses points.
4. **Show.** Results go to `data/results.json`. `report.py` serves the interactive page (three small JSON endpoints that call the same functions as the command line) and can also write a static snapshot.

## Performance

Full sweep, 107 targets, 19.5 million comparisons, one thread each, matching time only (on a 2-core cloud machine):

| | Time | Matches |
|---|---|---|
| Pure Python (same algorithm, line for line) | 120.7s | 1,508 |
| C++ | 1.9s | 1,508 |

That's 62x faster, with identical results. With two threads the C++ sweep takes 1.2s. Loading and indexing the name list adds about 0.6s.

## Testing

21 tests, no network needed:

- **Typo classification:** hand-written cases with known answers for every label (swap, keyboard slip, lookalike, separator, ...).
- **Distance limits:** short names allow one edit, long names two; exact matches and unrelated names never appear.
- **C++ vs Python cross-check:** 853 randomly mutated names (fixed seed) run through both implementations; the sets of matches must be identical.
- **Python logic:** PEP 503 normalization, requirements parsing (versions, extras, markers, comments, pip options), risk scoring, and metadata trimming against hand-built PyPI responses.

## Limitations

- **A high score means suspicious, not malicious.** Some flagged names are abandoned experiments, student projects, or names reserved by the real maintainers. Read the package before reporting it.
- **The worst cases are often already gone.** PyPI removes confirmed malware quickly, so a scan shows what's left, not everything that was ever uploaded.
- **The target list is hand-picked.** PyPI doesn't publish download rankings through an API this tool uses, so the 107 targets are well-known packages from the top of public download rankings.
- **Combosquatting isn't covered.** Names like `requests-py` or `py-pandas` add a whole word rather than a typo, and need a different approach.
- **Keyboard adjacency assumes QWERTY.**

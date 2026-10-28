#!/usr/bin/env python3
"""Typosquat Radar: find PyPI packages named to be mistaken for popular ones.

Commands
    radar.py fetch                    download every PyPI project name
    radar.py check <package>          list lookalikes of one package, ranked by risk
    radar.py scan <requirements.txt>  warn about lookalikes in your own dependencies
    radar.py sweep                    scan all popular targets, save results.json
    radar.py top                      rank the popular targets by risky lookalikes
    radar.py bench [n]                time the C++ matcher against pure Python
    radar.py ui                       open the interactive results page in a browser

Name matching runs in the C++ binary built from match.cpp (compiled on first
use). Everything else - downloading, caching, risk scoring, output - is here.
Only the Python standard library is used.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
NAMES_FILE = DATA / "names.txt"          # every normalized PyPI name, one per line
TARGETS_FILE = DATA / "targets.txt"      # the popular packages we protect
META_CACHE = DATA / "meta_cache.json"    # trimmed PyPI metadata, keyed by name
RESULTS_FILE = DATA / "results.json"     # output of `sweep`, input of report.py
MATCH_SRC = HERE / "match.cpp"
MATCH_BIN = HERE / ("match.exe" if sys.platform == "win32" else "match")   # g++ adds .exe on Windows

INDEX_URL = "https://pypi.org/simple/"
JSON_URL = "https://pypi.org/pypi/{name}/json"
USER_AGENT = "typosquat-radar/1.0 (research tool)"

# Popular, widely installed packages to protect. PyPI does not publish download
# rankings through an API this tool can reach, so this is a hand-picked list of
# well-known packages that sit near the top of public download rankings.
TARGETS = """
boto3 botocore urllib3 requests setuptools certifi idna charset-normalizer
typing-extensions python-dateutil packaging s3transfer six aiobotocore pyyaml
numpy grpcio-status pip fsspec s3fs cryptography pydantic cffi attrs pycparser
pandas importlib-metadata protobuf jmespath rsa pyasn1 platformdirs zipp click
wheel jinja2 markupsafe pytz filelock pyjwt tomli pluggy colorama virtualenv
pytest google-api-core pyarrow sqlalchemy wrapt requests-oauthlib oauthlib
iniconfig greenlet aiohttp multidict yarl frozenlist aiosignal tzdata scipy
psutil docutils pygments decorator pillow isodate beautifulsoup4 soupsieve
werkzeug flask django matplotlib tqdm openpyxl httpx httpcore anyio sniffio
h11 lxml exceptiongroup tomlkit rich regex distlib coverage scikit-learn
pydantic-core tenacity grpcio websocket-client more-itertools paramiko
pyopenssl gitpython fastapi uvicorn starlette selenium tensorflow torch
openai keras redis kubernetes docker sentry-sdk
""".split()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def normalize(name):
    """PEP 503 normalization: 'Typing_Extensions' and 'typing-extensions' are
    the same project on PyPI, so they must compare as equal here too."""
    return re.sub(r"[-_.]+", "-", name).lower()


def http_get(url, accept=None, retries=4):
    """GET with a polite user agent and exponential backoff on 429/5xx.
    Returns (status, body_bytes); 404 is returned, not raised."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            if e.code not in (429, 500, 502, 503, 504) or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(delay)          # back off, then try again
        delay *= 2
    raise RuntimeError("unreachable")


def ensure_binary():
    """Compile match.cpp the first time it is needed (or after it changes)."""
    if MATCH_BIN.exists() and MATCH_BIN.stat().st_mtime >= MATCH_SRC.stat().st_mtime:
        return
    print("compiling match.cpp ...", file=sys.stderr)
    subprocess.run(["g++", "-O2", "-std=c++17", "-pthread", str(MATCH_SRC), "-o", str(MATCH_BIN)], check=True)


def ensure_names():
    if not NAMES_FILE.exists():
        sys.exit("no name list yet: run `python3 radar.py fetch` first")


# --------------------------------------------------------------------------
# fetch: download the full list of project names
# --------------------------------------------------------------------------

def cmd_fetch(_args):
    DATA.mkdir(exist_ok=True)
    print("downloading the PyPI project index ...")
    t0 = time.time()
    # The JSON form of the "simple" index lists every project name in one response.
    _, body = http_get(INDEX_URL, accept="application/vnd.pypi.simple.v1+json")
    projects = json.loads(body)["projects"]
    names = sorted({normalize(p["name"]) for p in projects})
    NAMES_FILE.write_text("\n".join(names) + "\n", encoding="utf-8")
    TARGETS_FILE.write_text("\n".join(sorted(set(normalize(t) for t in TARGETS))) + "\n", encoding="utf-8")
    print(f"saved {len(names):,} names ({len(body) / 1e6:.0f} MB index) in {time.time() - t0:.1f}s")


# --------------------------------------------------------------------------
# Matching: hand names to the C++ binary, read back candidates
# --------------------------------------------------------------------------

def write_temp_list(items):
    """Write one item per line to a fresh temp file and return its path. Each
    call gets its own file, so the web UI can run several checks at once."""
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", dir=DATA, delete=False, encoding="utf-8")
    f.write("\n".join(items) + "\n")
    f.close()
    return Path(f.name)


def run_matcher(names_path, targets, threads=None):
    """Return (rows, stats). Each row: target, candidate, distance, kind, detail."""
    ensure_binary()
    tfile = write_temp_list(targets)
    cmd = [str(MATCH_BIN), str(names_path), str(tfile)]
    if threads:
        cmd.append(str(threads))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
    finally:
        tfile.unlink()
    rows = []
    for line in proc.stdout.splitlines():
        target, cand, dist, kind, detail = line.split("\t")
        rows.append({"target": target, "name": cand, "distance": int(dist), "kind": kind, "detail": detail})
    stats = dict(kv.split("=") for kv in proc.stderr.split())
    return rows, {k: int(v) for k, v in stats.items()}


# --------------------------------------------------------------------------
# Metadata + risk scoring
# --------------------------------------------------------------------------

def trim_metadata(doc):
    """Keep only the fields the risk score needs from a PyPI JSON document."""
    info = doc.get("info") or {}
    uploads, sizes = [], []
    releases_with_files = 0
    for files in (doc.get("releases") or {}).values():
        if files:
            releases_with_files += 1
        for f in files:
            uploads.append(f["upload_time_iso_8601"])
    for f in doc.get("urls") or []:                  # files of the latest release
        sizes.append(f.get("size") or 0)
    urls = info.get("project_urls") or {}
    return {
        "exists": True,
        "summary": (info.get("summary") or "").strip(),
        "has_links": bool(info.get("home_page") or any(urls.values())),
        "releases": releases_with_files,
        "first_upload": min(uploads) if uploads else None,
        "last_upload": max(uploads) if uploads else None,
        "latest_size": max(sizes) if sizes else 0,
    }


CACHE_LOCK = threading.Lock()      # the web UI can call fetch_metadata from several threads


def load_cache():
    return json.loads(META_CACHE.read_text(encoding="utf-8")) if META_CACHE.exists() else {}


def fetch_metadata(names, workers=8):
    """Metadata for each name, from cache when possible, else PyPI (in parallel)."""
    with CACHE_LOCK:
        cache = load_cache()
    missing = [n for n in set(names) if n not in cache]

    def one(name):
        status, body = http_get(JSON_URL.format(name=name))
        if status == 404:                        # listed in the index but no JSON: empty project
            return name, {"exists": False}
        return name, trim_metadata(json.loads(body))

    if missing:
        print(f"fetching metadata for {len(missing):,} packages ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, (name, meta) in enumerate(pool.map(one, missing), 1):
                cache[name] = meta
                if i % 500 == 0:
                    print(f"  {i:,}/{len(missing):,}", file=sys.stderr)
                    save_cache(cache)                          # checkpoint long runs
        save_cache(cache)
    return {n: cache[n] for n in names}


def save_cache(new_entries):
    """Merge into the on-disk cache (another thread may have added entries)."""
    with CACHE_LOCK:
        cache = load_cache()
        cache.update(new_entries)
        META_CACHE.write_text(json.dumps(cache), encoding="utf-8")


def days_since(iso, now=None):
    if not iso:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


# Kinds of typo that only make sense as an attempt to look like the target.
DECEPTIVE_KINDS = {"lookalike", "swap", "keyboard", "double", "separator"}


def risk_score(meta, kind, now=None):
    """0-100 suspicion score plus the reasons behind it.
    New, empty, anonymous packages with deceptive names score high; old,
    actively released projects score low even if their name is close."""
    if not meta.get("exists"):
        return 20, ["no releases (name reserved or emptied)"]
    score, reasons = 0, []
    age = days_since(meta["first_upload"], now)
    if age is not None and age <= 180:
        score += 30; reasons.append(f"first upload {age} days ago")
    elif age is not None and age <= 730:
        score += 15; reasons.append("first upload under 2 years ago")
    if meta["releases"] <= 1:
        score += 20; reasons.append("only 1 release")
    elif meta["releases"] <= 3:
        score += 10; reasons.append(f"only {meta['releases']} releases")
    if not meta["summary"]:
        score += 15; reasons.append("no description")
    if not meta["has_links"]:
        score += 15; reasons.append("no homepage or repo link")
    if 0 < meta["latest_size"] < 10_000:
        score += 10; reasons.append(f"tiny package ({meta['latest_size'] / 1000:.0f} KB)")
    if kind in DECEPTIVE_KINDS:
        score += 10; reasons.append("deceptive typo type")
    if age is not None and age > 5 * 365 and meta["releases"] >= 10:
        score -= 30; reasons.append("established project")
    return max(0, min(100, score)), reasons


def level(score):
    return "HIGH" if score >= 60 else "MEDIUM" if score >= 35 else "LOW"


def enrich(rows):
    """Attach metadata, score, and level to matcher rows; highest risk first."""
    metas = fetch_metadata([r["name"] for r in rows])
    for r in rows:
        r["meta"] = metas[r["name"]]
        r["score"], r["reasons"] = risk_score(r["meta"], r["kind"])
        r["level"] = level(r["score"])
    return sorted(rows, key=lambda r: (-r["score"], r["distance"], r["name"]))


def describe_age(meta):
    if not meta.get("exists") or not meta.get("first_upload"):
        return "no releases"
    d = days_since(meta["first_upload"])
    if d < 60:
        return f"registered {d} days ago"
    if d < 730:
        return f"registered {d // 30} months ago"
    return f"registered {meta['first_upload'][:4]}"


# --------------------------------------------------------------------------
# check: one package
# --------------------------------------------------------------------------

def check_package(package):
    """Lookalikes of one package (any name, not only the popular targets),
    scored and sorted. Shared by the CLI and the web UI."""
    ensure_names()
    target = normalize(package)
    rows, stats = run_matcher(NAMES_FILE, [target])
    return {"target": target, "names_scanned": stats["names"], "match_ms": stats["match_ms"],
            "rows": enrich(rows)}


def open_ui(args, start, scan_text=None):
    """After a command finishes, open the interactive page on the matching view
    (skipped with --no-ui, e.g. in scripts or CI)."""
    if getattr(args, "no_ui", False):
        return
    import report
    report.serve(port=args.port, start=start, scan_text=scan_text)


def cmd_check(args):
    result = check_package(args.package)
    rows = result["rows"]
    print(f"Scanned {result['names_scanned']:,} names in {result['match_ms'] / 1000:.2f}s, "
          f"{len(rows)} lookalikes of {result['target']}\n")
    shown = [r for r in rows if r["level"] != "LOW"] if not args.all else rows
    for r in shown[: args.limit]:
        m = r["meta"]
        rel = f"{m.get('releases', 0)} release{'s' if m.get('releases', 0) != 1 else ''}"
        print(f"  {r['name']:<22} {r['detail']:<30} {describe_age(m):<24} {rel:<12} {r['level']:<6} {r['score']:>3}")
    hidden = len(rows) - len(shown[: args.limit])
    if hidden:
        print(f"\n  ({hidden} more not shown here; all of them are on the page)")
    open_ui(args, "pkg=" + result["target"])


# --------------------------------------------------------------------------
# scan: a requirements file
# --------------------------------------------------------------------------

def parse_requirements(text):
    """Pull bare project names out of a requirements file, ignoring versions,
    extras, environment markers, comments, and pip options."""
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):         # blank, comment, or -r / -e / --index-url
            continue
        m = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", line)
        if m:
            names.append(normalize(m.group(0)))
    return names


def scan_requirements(text):
    """Check every requirement in `text`. Returns one dict per requirement with
    a status of OK, HIGH, MEDIUM, LOW, or MISSING. Shared by the CLI and the UI."""
    ensure_names()
    reqs = parse_requirements(text)
    targets = set(TARGETS_FILE.read_text(encoding="utf-8").split())
    on_pypi = set(NAMES_FILE.read_text(encoding="utf-8").split())

    # Reuse the matcher backwards: the requirements are the "names" to search,
    # and the popular packages are the targets they might be imitating.
    rfile = write_temp_list(reqs)
    try:
        rows, _ = run_matcher(rfile, sorted(targets))
    finally:
        rfile.unlink()
    flagged = {}
    for r in rows:
        if r["name"] not in targets:                 # "urllib3" is near "urllib3-..." but is itself real
            flagged.setdefault(r["name"], r)
    metas = fetch_metadata([n for n in flagged if n in on_pypi])

    results = []
    for name in dict.fromkeys(reqs):                 # keep file order, drop duplicates
        r = flagged.get(name)
        if name not in on_pypi:
            # A name that isn't on PyPI is a trap waiting to be set: the install
            # fails today, but someone could register it tomorrow.
            results.append({"name": name, "status": "MISSING", "target": r and r["target"],
                            "detail": r and r["detail"],
                            "reasons": ["not on PyPI; installs fail today and an attacker could try to register it"]})
        elif r:
            score, reasons = risk_score(metas[name], r["kind"])
            results.append({"name": name, "status": level(score), "score": score, "target": r["target"],
                            "detail": r["detail"], "reasons": reasons or ["no warning signs"]})
        else:
            results.append({"name": name, "status": "OK"})
    return results


def cmd_scan(args):
    text = Path(args.file).read_text(encoding="utf-8")
    results = scan_requirements(text)
    ok = [r["name"] for r in results if r["status"] == "OK"]
    print(f"  OK       {', '.join(ok) if ok else '-'}")
    for r in results:
        if r["status"] == "OK":
            continue
        like = f"{r['detail']} of \"{r['target']}\"; " if r.get("target") else ""
        print(f"  {r['status']:<8} {r['name']}  <- {like}{', '.join(r['reasons'])}")
    open_ui(args, "scan", scan_text=text)
    # Non-zero exit lets CI fail the build on anything high risk or missing.
    return 1 if any(r["status"] in ("HIGH", "MISSING") for r in results) else 0


# --------------------------------------------------------------------------
# sweep: every target, saved for the report
# --------------------------------------------------------------------------

def cmd_sweep(args):
    ensure_names()
    targets = TARGETS_FILE.read_text(encoding="utf-8").split()
    rows, stats = run_matcher(NAMES_FILE, targets)
    print(f"matcher: {stats['comparisons']:,} comparisons over {stats['names']:,} names, "
          f"{len(rows):,} lookalikes of {len(targets)} targets in {stats['match_ms'] / 1000:.2f}s")
    rows = enrich(rows)
    new = [r for r in rows if (days_since(r["meta"].get("first_upload")) or 10**9) <= 90]
    summary = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "names_scanned": stats["names"],
        "targets": len(targets),
        "comparisons": stats["comparisons"],
        "matcher_ms": stats["match_ms"],
        "total_ms": stats["ms"],
        "lookalikes": len(rows),
        "high": sum(r["level"] == "HIGH" for r in rows),
        "medium": sum(r["level"] == "MEDIUM" for r in rows),
        "registered_last_90_days": len(new),
        "by_kind": {k: sum(r["kind"] == k for r in rows) for k in sorted({r["kind"] for r in rows})},
    }
    RESULTS_FILE.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    open_ui(args, "map")


# --------------------------------------------------------------------------
# bench: C++ vs the same algorithm in pure Python
# --------------------------------------------------------------------------

def py_osa_distance(a, b, limit):
    """Line-for-line Python port of osa_distance() in match.cpp."""
    n, m = len(a), len(b)
    if abs(n - m) > limit:
        return limit + 1
    prev2, prev = [0] * (m + 1), list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            best = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                best = min(best, prev2[j - 2] + 1)
            cur[j] = best
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[m]


def cmd_bench(args):
    ensure_names()
    names = NAMES_FILE.read_text(encoding="utf-8").split()
    targets = TARGETS_FILE.read_text(encoding="utf-8").split()[: args.n]
    by_len = {}
    for nm in names:
        by_len.setdefault(len(nm), []).append(nm)

    t0 = time.perf_counter()
    py_hits = 0
    for t in targets:
        limit = 2 if len(t) >= 10 else 1
        for length in range(len(t) - limit, len(t) + limit + 1):
            for nm in by_len.get(length, []):
                if nm != t and py_osa_distance(nm, t, limit) <= limit:
                    py_hits += 1
    py_s = time.perf_counter() - t0

    # Same targets, single thread, so the comparison is fair. Only distance
    # matches are counted (the C++ side also reports skeleton lookalikes).
    # Timing covers matching only on both sides: Python's length buckets are
    # built before its clock starts, and match.cpp reports match_ms separately
    # from the time it spends reading and indexing the name list.
    rows, stats = run_matcher(NAMES_FILE, targets, threads=1)
    cpp_hits = sum(r["distance"] > 0 for r in rows)
    cpp_s = max(stats["match_ms"], 1) / 1000
    print(f"targets: {len(targets)}   comparisons: {stats['comparisons']:,}")
    print(f"python : {py_s:7.2f}s  ({py_hits} matches)")
    print(f"c++    : {cpp_s:7.2f}s  ({cpp_hits} matches, 1 thread; plus {(stats['ms'] - stats['match_ms']) / 1000:.2f}s loading)")
    print(f"speedup: {py_s / cpp_s:.0f}x   results agree: {py_hits == cpp_hits}")


def cmd_top(args):
    """Print the popular packages ranked by how many risky lookalikes they have."""
    if not RESULTS_FILE.exists():
        sys.exit("no sweep results yet: run `python3 radar.py sweep` first")
    rows = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))["rows"]
    counts = {}
    for r in rows:
        c = counts.setdefault(r["target"], {"HIGH": 0, "MEDIUM": 0, "LOW": 0})
        c[r["level"]] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1]["HIGH"], -kv[1]["MEDIUM"], kv[0]))
    print(f"  {'package':<22} {'high':>5} {'medium':>7} {'low':>5} {'total':>6}")
    for name, c in ranked[: args.limit]:
        print(f"  {name:<22} {c['HIGH']:>5} {c['MEDIUM']:>7} {c['LOW']:>5} {sum(c.values()):>6}")
    open_ui(args, "map")


def cmd_ui(args):
    import report                     # report.py holds the page and the small web server
    report.serve(port=args.port, open_browser=not args.no_browser)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch").set_defaults(func=cmd_fetch)
    c = sub.add_parser("check"); c.add_argument("package")
    c.add_argument("--all", action="store_true", help="include LOW-risk matches")
    c.add_argument("--limit", type=int, default=15); c.set_defaults(func=cmd_check)
    s = sub.add_parser("scan"); s.add_argument("file"); s.set_defaults(func=cmd_scan)
    w = sub.add_parser("sweep"); w.set_defaults(func=cmd_sweep)
    t = sub.add_parser("top"); t.add_argument("--limit", type=int, default=25); t.set_defaults(func=cmd_top)
    for sp in (c, s, w, t):      # these open the page when done unless --no-ui
        sp.add_argument("--no-ui", action="store_true", help="print results only, don't open the page")
        sp.add_argument("--port", type=int, default=8765)
    u = sub.add_parser("ui"); u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true"); u.set_defaults(func=cmd_ui)
    b = sub.add_parser("bench"); b.add_argument("n", nargs="?", type=int, default=5); b.set_defaults(func=cmd_bench)
    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

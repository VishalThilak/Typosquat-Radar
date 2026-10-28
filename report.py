#!/usr/bin/env python3
"""Results viewers for Typosquat Radar.

    python3 radar.py ui          interactive page served locally (opens your browser)
    python3 report.py [out.html] static, self-contained snapshot of the last sweep

The interactive page (serve() below) runs a tiny local web server from the
standard library. It answers three JSON endpoints using radar.py's own
functions, and the page draws everything in the browser:
    GET  /api/overview        popular packages and their counts from the last sweep
    GET  /api/check?name=x    live lookalike check of any package name
    POST /api/scan            requirements text in, per-requirement verdicts out

The static snapshot (build() below) has three parts: headline numbers, an "orbit map" (each popular
package in the middle of a small chart, its lookalikes circling it, colored by
risk, hover for details), and a table of the highest-risk names. No external
scripts or stylesheets, so the file opens anywhere, offline.
"""

import html
import json
import math
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "data" / "results.json"

SIZE = 180                      # each orbit chart is SIZE x SIZE pixels
RING = {0: 34, 1: 58, 2: 80}    # ring radius by edit distance (0 = lookalike characters)
COLOR = {"HIGH": "var(--high)", "MEDIUM": "var(--medium)", "LOW": "var(--low)"}


def esc(s):
    return html.escape(str(s), quote=True)


def tooltip(r):
    """Text shown when hovering a dot."""
    m = r["meta"]
    first = (m.get("first_upload") or "")[:10] or "no releases"
    reasons = "; ".join(r["reasons"]) or "no warning signs"
    return f"{r['name']} | {r['detail']} | first upload {first} | {m.get('releases', 0)} releases | {r['level']} {r['score']}: {reasons}"


def orbit_svg(target, rows):
    """One small radial chart: the target in the middle, lookalikes on rings."""
    c = SIZE / 2
    parts = [f'<svg viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}" role="img" '
             f'aria-label="{esc(target)}: {len(rows)} lookalikes">']
    for radius in RING.values():                                  # faint guide rings
        parts.append(f'<circle cx="{c}" cy="{c}" r="{radius}" class="ring"/>')

    # Spread the dots of each ring evenly around it, riskiest first so the
    # important ones never end up hidden underneath a crowd.
    by_ring = {}
    for r in sorted(rows, key=lambda r: r["score"]):
        by_ring.setdefault(r["distance"], []).append(r)
    for dist, ring_rows in by_ring.items():
        radius = RING.get(dist, RING[2])
        for i, r in enumerate(ring_rows):
            angle = 2 * math.pi * i / len(ring_rows) - math.pi / 2
            x, y = c + radius * math.cos(angle), c + radius * math.sin(angle)
            dot = 5.5 if r["level"] == "HIGH" else 4 if r["level"] == "MEDIUM" else 3
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{dot}" fill="{COLOR[r["level"]]}" '
                         f'class="dot" data-tip="{esc(tooltip(r))}"/>')

    parts.append(f'<circle cx="{c}" cy="{c}" r="15" class="core"/>')
    label = target if len(target) <= 14 else target[:13] + "…"
    parts.append(f'<text x="{c}" y="{SIZE - 6}" text-anchor="middle" class="label">{esc(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build(results):
    s, rows = results["summary"], results["rows"]

    # Group lookalikes under the package they imitate; busiest-risk targets first.
    by_target = {}
    for r in rows:
        by_target.setdefault(r["target"], []).append(r)
    order = sorted(by_target, key=lambda t: (-sum(r["level"] == "HIGH" for r in by_target[t]),
                                             -len(by_target[t]), t))
    cards = "".join(
        f'<figure class="card">{orbit_svg(t, by_target[t])}'
        f'<figcaption>{len(by_target[t])} lookalikes · '
        f'{sum(r["level"] == "HIGH" for r in by_target[t])} high</figcaption></figure>'
        for t in order)

    top = [r for r in rows if r["level"] != "LOW"][:60]
    table_rows = "".join(
        f'<tr><td><a href="https://pypi.org/project/{esc(r["name"])}/">{esc(r["name"])}</a></td>'
        f'<td>{esc(r["target"])}</td><td>{esc(r["detail"])}</td>'
        f'<td>{esc((r["meta"].get("first_upload") or "")[:10] or "none")}</td>'
        f'<td class="num">{r["meta"].get("releases", 0)}</td>'
        f'<td><span class="pill {r["level"].lower()}">{r["level"]} {r["score"]}</span></td>'
        f'<td class="why">{esc("; ".join(r["reasons"]))}</td></tr>'
        for r in top)

    kinds = ", ".join(f"{k} {v}" for k, v in sorted(s["by_kind"].items(), key=lambda kv: -kv[1]))
    stat = lambda label, value: f'<div class="stat"><span>{label}</span><b>{value}</b></div>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Typosquat Radar</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a64; --line:#e3e1da; --card:#ffffff;
        --high:#d0452f; --medium:#d99a1e; --low:#b9b7ae; --core:#1d1d1b; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#171716; --fg:#ecebe6; --muted:#9c9a92; --line:#33322f; --card:#1f1f1d;
          --high:#f06a52; --medium:#f0b340; --low:#5c5b56; --core:#ecebe6; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 ui-sans-serif,system-ui,sans-serif }}
main {{ max-width:1100px; margin:0 auto; padding:32px 16px 64px }}
h1 {{ font-size:28px; margin:0 0 4px }} h2 {{ font-size:18px; margin:40px 0 12px }}
.sub {{ color:var(--muted); margin:0 0 24px }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px }}
.stat span {{ display:block; color:var(--muted); font-size:13px }} .stat b {{ font-size:22px; font-weight:600 }}
.legend {{ color:var(--muted); font-size:13px; margin-bottom:12px }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin:0 4px 0 12px; vertical-align:-1px }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax({SIZE}px,1fr)); gap:10px }}
.card {{ margin:0; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:6px; text-align:center }}
.card svg {{ max-width:100%; height:auto }}
figcaption {{ font-size:12px; color:var(--muted) }}
.ring {{ fill:none; stroke:var(--line) }} .core {{ fill:var(--core) }}
.label {{ fill:var(--fg); font-size:12px; font-weight:600 }}
.dot {{ cursor:pointer }} .dot:hover {{ stroke:var(--fg); stroke-width:2 }}
#tip {{ position:absolute; display:none; max-width:340px; background:var(--fg); color:var(--bg);
       font-size:12px; padding:8px 10px; border-radius:6px; pointer-events:none; z-index:5 }}
.tablewrap {{ overflow-x:auto }}
table {{ width:100%; border-collapse:collapse; font-size:13px }}
th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top }}
th {{ color:var(--muted); font-weight:500 }} td.num {{ text-align:right }}
td.why {{ color:var(--muted) }} a {{ color:inherit }}
.pill {{ font-size:12px; padding:2px 7px; border-radius:99px; white-space:nowrap; color:#fff }}
.pill.high {{ background:var(--high) }} .pill.medium {{ background:var(--medium); color:#2a1d00 }}
.note {{ color:var(--muted); font-size:13px; margin-top:32px }}
</style></head><body><main>
<h1>Typosquat Radar</h1>
<p class="sub">Lookalike names of {s['targets']} popular PyPI packages, scanned {esc(s['generated'][:10])}.</p>
<div class="stats">
{stat("Names scanned", f"{s['names_scanned']:,}")}
{stat("Comparisons", f"{s['comparisons']:,}")}
{stat("Matching time", f"{s['matcher_ms'] / 1000:.1f}s")}
{stat("Lookalikes", f"{s['lookalikes']:,}")}
{stat("High risk", s['high'])}
{stat("Registered in last 90 days", s['registered_last_90_days'])}
</div>

<h2>Orbit map</h2>
<div class="legend">Inner ring: lookalike characters. Middle: one edit. Outer: two edits.
<i style="background:var(--high)"></i>high<i style="background:var(--medium)"></i>medium<i style="background:var(--low)"></i>low.
Hover a dot for details.</div>
<div class="grid">{cards}</div>

<h2>Highest-risk names</h2>
<div class="tablewrap"><table>
<thead><tr><th>Name</th><th>Imitates</th><th>Typo</th><th>First upload</th><th>Releases</th><th>Risk</th><th>Why</th></tr></thead>
<tbody>{table_rows}</tbody></table></div>

<p class="note">Typo types found: {esc(kinds)}.<br>
A high score means a name looks suspicious, not that the package is proven malicious.
Many lookalikes are abandoned experiments or names reserved by the real maintainers.
PyPI also removes confirmed malware quickly, so the worst cases are usually already gone.</p>
</main>
<div id="tip"></div>
<script>
// One shared tooltip, moved to the mouse and filled from the dot's data-tip.
const tip = document.getElementById("tip");
document.querySelectorAll(".dot").forEach(dot => {{
  dot.addEventListener("mousemove", e => {{
    tip.textContent = dot.dataset.tip;
    tip.style.display = "block";
    tip.style.left = Math.min(e.pageX + 12, window.innerWidth - 360) + "px";
    tip.style.top = (e.pageY + 12) + "px";
  }});
  dot.addEventListener("mouseleave", () => {{ tip.style.display = "none"; }});
}});
</script></body></html>"""


# --------------------------------------------------------------------------
# Interactive page: `python3 radar.py ui`
# --------------------------------------------------------------------------

def overview():
    """Popular targets with counts from the last sweep, plus compact rows for
    the map. Works before any sweep too (just names, no counts)."""
    import radar
    targets = radar.TARGETS_FILE.read_text(encoding="utf-8").split() if radar.TARGETS_FILE.exists() else []
    if not RESULTS.exists():
        return {"summary": None, "targets": [{"name": t} for t in targets], "rows": [], "scan": INITIAL["scan"]}
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    counts = {t: {"name": t, "HIGH": 0, "MEDIUM": 0, "LOW": 0} for t in targets}
    for r in data["rows"]:
        counts.setdefault(r["target"], {"name": r["target"], "HIGH": 0, "MEDIUM": 0, "LOW": 0})[r["level"]] += 1
    ranked = sorted(counts.values(), key=lambda c: (-c["HIGH"], -c["MEDIUM"], c["name"]))
    rows = [{"target": r["target"], "name": r["name"], "distance": r["distance"], "level": r["level"],
             "score": r["score"], "detail": r["detail"]} for r in data["rows"]]
    return {"summary": data["summary"], "targets": ranked, "rows": rows, "scan": INITIAL["scan"]}


class Handler(BaseHTTPRequestHandler):
    """Routes for the local UI. Every response is either the page or JSON."""

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        import radar
        url = urlparse(self.path)
        try:
            if url.path == "/":
                body = APP_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/overview":
                self.send_json(overview())
            elif url.path == "/api/check":
                name = (parse_qs(url.query).get("name") or [""])[0].strip()
                if not name:
                    return self.send_json({"error": "Enter a package name"}, 400)
                self.send_json(radar.check_package(name))
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as e:                    # show the problem in the page, keep serving
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        import radar
        if urlparse(self.path).path != "/api/scan":
            return self.send_json({"error": "not found"}, 404)
        try:
            text = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            self.send_json({"results": radar.scan_requirements(text)})
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def log_message(self, *args):                # keep the terminal quiet
        pass


INITIAL = {"scan": None}     # requirements text from `radar.py scan`, shown pre-filled in the page


def serve(port=8765, open_browser=True, start="", scan_text=None):
    """Run the local page. `start` is the view to open on (e.g. "pkg=pandas",
    "map", "scan"); `scan_text` pre-fills and runs the requirements scan."""
    import radar
    INITIAL["scan"] = scan_text
    if not radar.NAMES_FILE.exists():            # first run: get the name list automatically
        radar.cmd_fetch(None)
    for p in range(port, port + 20):              # step past ports that are already taken
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        sys.exit(f"no free port between {port} and {port + 19}")
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"\nTyposquat Radar page open at {url}  (Ctrl+C to stop)")
    if start:
        url += "#" + start
    if not RESULTS.exists():
        print("tip: run `python3 radar.py sweep` once to fill in the popular-package counts and the map")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# The whole interactive page: markup, styles, and script in one string. Plain
# (not an f-string) so the JavaScript braces need no escaping; all data
# arrives from the JSON endpoints above.
APP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Typosquat Radar</title>
<style>
:root { --bg:#f6f4ef; --panel:#fffefb; --fg:#1d1d1b; --muted:#6b6a64; --line:#e3e1da; --soft:#efede6;
        --high:#d0452f; --medium:#d99a1e; --low:#bcbab1; --core:#1d1d1b; --core-fg:#f6f4ef; --ok:#2f8f5b;
        --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141413; --panel:#1c1c1a; --fg:#ecebe6; --muted:#9c9a92; --line:#2f2e2b; --soft:#252523;
          --high:#f06a52; --medium:#f0b340; --low:#55544f; --core:#ecebe6; --core-fg:#141413; --ok:#5fc48c; } }
* { box-sizing:border-box }
html, body { margin:0; height:100% }
body { background:var(--bg); color:var(--fg); font:14px/1.5 ui-sans-serif, system-ui, sans-serif }
button, input, textarea { font:inherit; color:inherit }
.app { display:grid; grid-template-columns:280px minmax(0,1fr); height:100vh }
aside { border-right:1px solid var(--line); background:var(--panel); display:flex; flex-direction:column; min-height:0 }
.brand { padding:18px 18px 10px }
.brand h1 { font-size:18px; margin:0 } .brand p { margin:2px 0 0; color:var(--muted); font-size:12px }
.search { padding:8px 18px 12px; border-bottom:1px solid var(--line) }
.search form { display:flex; gap:6px }
input[type=text] { flex:1; min-width:0; height:34px; padding:0 10px; border:1px solid var(--line); border-radius:8px; background:var(--bg); font-family:var(--mono) }
input[type=text]:focus, textarea:focus { outline:2px solid var(--fg); outline-offset:-1px }
.btn { height:34px; padding:0 12px; border:1px solid var(--fg); background:var(--fg); color:var(--bg); border-radius:8px; cursor:pointer }
.btn.ghost { background:transparent; color:var(--fg); border-color:var(--line) }
.listhead { display:flex; justify-content:space-between; padding:12px 18px 6px; color:var(--muted); font-size:12px }
.list { overflow-y:auto; flex:1; padding:0 8px 12px }
.item { display:flex; align-items:center; gap:8px; width:100%; padding:6px 10px; border:0; background:none; border-radius:6px; cursor:pointer; text-align:left }
.item:hover { background:var(--soft) } .item.on { background:var(--fg); color:var(--bg) }
.item .nm { flex:1; font-family:var(--mono); font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
.cnt { font-size:11px; min-width:22px; text-align:center; padding:0 5px; border-radius:99px; color:#fff }
.cnt.h { background:var(--high) } .cnt.m { background:var(--medium); color:#2a1d00 } .cnt.z { background:transparent; color:var(--muted) }
main { overflow-y:auto; min-height:0 }
.top { display:flex; gap:4px; padding:14px 28px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:2 }
.tab { border:0; background:none; padding:6px 12px; border-radius:6px; cursor:pointer; color:var(--muted) }
.tab.on { background:var(--panel); color:var(--fg); box-shadow:0 0 0 1px var(--line) }
.view { padding:24px 28px 60px; max-width:1100px }
.stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(140px,1fr)); gap:8px; margin-bottom:22px }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 12px }
.stat span { display:block; color:var(--muted); font-size:12px } .stat b { font-size:20px; font-weight:600 }
h2 { font-size:22px; margin:0; font-family:var(--mono); font-weight:600 }
.sub { color:var(--muted); margin:2px 0 18px }
.pkg { display:grid; grid-template-columns:340px minmax(0,1fr); gap:24px; align-items:start }
.orbitbox { position:sticky; top:72px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:10px }
.orbitbox svg { width:100%; height:auto; display:block }
.legend { display:flex; flex-wrap:wrap; gap:4px 14px; color:var(--muted); font-size:12px; padding:4px 6px 2px }
.legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px }
.chips { display:flex; gap:6px; margin-bottom:10px; flex-wrap:wrap }
.chip { border:1px solid var(--line); background:var(--panel); border-radius:99px; padding:3px 11px; cursor:pointer; font-size:13px }
.chip.on { background:var(--fg); color:var(--bg); border-color:var(--fg) }
.rows { display:flex; flex-direction:column; gap:6px }
.row { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px 12px; display:grid;
       grid-template-columns:minmax(0,1fr) auto; gap:2px 12px; transition:box-shadow .15s }
.row.hot { box-shadow:0 0 0 2px var(--fg) }
.row a { font-family:var(--mono); font-weight:600; color:inherit; text-decoration:none } .row a:hover { text-decoration:underline }
.row .kind { color:var(--muted); margin-left:8px; font-size:13px }
.row .meta, .row .why { grid-column:1 / -1; color:var(--muted); font-size:12.5px }
.pill { font-size:12px; padding:2px 8px; border-radius:99px; white-space:nowrap; color:#fff; align-self:start }
.pill.HIGH { background:var(--high) } .pill.MEDIUM { background:var(--medium); color:#2a1d00 }
.pill.LOW { background:var(--soft); color:var(--muted) } .pill.OK { background:var(--ok) } .pill.MISSING { background:var(--fg); color:var(--bg) }
.dot { cursor:pointer; transition:r .15s } .dot.hot { stroke:var(--fg); stroke-width:2.5 }
.ring { fill:none; stroke:var(--line) } .core { fill:var(--core) }
.coretext { fill:var(--core-fg); font-size:20px; font-weight:600 } .coresub { fill:var(--core-fg); font-size:10px }
.maplabel { fill:var(--fg); font-size:12px; font-weight:600; font-family:var(--mono) }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(170px,1fr)); gap:10px }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:6px; cursor:pointer; text-align:center }
.card:hover { border-color:var(--fg) } .card small { color:var(--muted); font-size:12px }
textarea { width:100%; min-height:160px; padding:10px; border:1px solid var(--line); border-radius:10px; background:var(--panel); font-family:var(--mono); font-size:13px }
.empty { color:var(--muted); padding:40px 0 } .err { color:var(--high) }
.spin { display:inline-block; width:12px; height:12px; border:2px solid var(--line); border-top-color:var(--fg); border-radius:50%; animation:s 0.8s linear infinite; vertical-align:-1px; margin-right:6px }
@keyframes s { to { transform:rotate(360deg) } }
#tip { position:fixed; display:none; max-width:300px; background:var(--fg); color:var(--bg); font-size:12px; padding:7px 9px; border-radius:6px; pointer-events:none; z-index:10 }
@media (max-width:820px) { .app { grid-template-columns:1fr; height:auto } aside { max-height:45vh } .pkg { grid-template-columns:1fr } .orbitbox { position:static } }
</style></head>
<body>
<div class="app">
  <aside>
    <div class="brand"><h1>Typosquat Radar</h1><p id="brandsub">PyPI lookalike scanner</p></div>
    <div class="search">
      <form id="checkform"><input type="text" id="checkname" placeholder="scikit-learn" aria-label="Check any package" autocomplete="off"><button class="btn">Check</button></form>
      <div id="checkerr" class="err" style="font-size:12px"></div>
    </div>
    <div class="listhead"><span>Popular packages</span><span>high · med</span></div>
    <div style="padding:0 18px 8px"><input type="text" id="filter" placeholder="Filter list" aria-label="Filter popular packages" style="height:30px;width:100%"></div>
    <div class="list" id="list"></div>
  </aside>
  <main>
    <div class="top">
      <button class="tab" data-tab="pkg">Package</button>
      <button class="tab" data-tab="map">Ecosystem map</button>
      <button class="tab" data-tab="scan">Scan requirements</button>
    </div>
    <div class="view" id="view"></div>
  </main>
</div>
<div id="tip"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const COLORS = {HIGH:"var(--high)", MEDIUM:"var(--medium)", LOW:"var(--low)"};
const state = { ov:null, tab:"pkg", pkg:null, result:null, filter:"ALL", scanText:null, scanResults:null };

// ---------- orbit chart: target in the middle, lookalikes on rings by edit distance ----------
function orbitSVG(rows, size, opts = {}) {
  const c = size / 2, R = {0: size*0.20, 1: size*0.32, 2: size*0.44};
  let out = `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="${esc(opts.label || "")}">`;
  for (const r of Object.values(R)) out += `<circle cx="${c}" cy="${c}" r="${r}" class="ring"/>`;
  const rings = {};
  rows.forEach((r, i) => (rings[r.distance] ??= []).push([r, i]));
  for (const [d, list] of Object.entries(rings)) {
    list.sort((a, b) => a[0].score - b[0].score);          // riskiest drawn last, on top
    const rad = R[d] ?? R[2];
    list.forEach(([r, i], k) => {
      const a = 2 * Math.PI * k / list.length - Math.PI / 2;
      const s = (r.level === "HIGH" ? 1.6 : r.level === "MEDIUM" ? 1.2 : 0.85) * size / 100 * (opts.big ? 1.4 : 1.6);
      out += `<circle class="dot" data-i="${i}" cx="${(c + rad*Math.cos(a)).toFixed(1)}" cy="${(c + rad*Math.sin(a)).toFixed(1)}" r="${s.toFixed(1)}" fill="${COLORS[r.level]}"/>`;
    });
  }
  out += `<circle cx="${c}" cy="${c}" r="${size*0.09}" class="core"/>`;
  if (opts.big) out += `<text x="${c}" y="${c+3}" text-anchor="middle" class="coretext">${rows.length}</text><text x="${c}" y="${c+16}" text-anchor="middle" class="coresub">lookalikes</text>`;
  if (opts.label && !opts.big) out += `<text x="${c}" y="${size-5}" text-anchor="middle" class="maplabel">${esc(opts.label.length > 15 ? opts.label.slice(0,14)+"…" : opts.label)}</text>`;
  return out + "</svg>";
}

function ago(iso) {
  if (!iso) return "no releases";
  const d = Math.floor((Date.now() - Date.parse(iso)) / 864e5);
  if (d < 60) return `registered ${d} days ago`;
  if (d < 730) return `registered ${Math.floor(d/30)} months ago`;
  return `registered ${iso.slice(0,4)}`;
}

function tip(e, text) { const t = $("#tip"); t.textContent = text; t.style.display = "block";
  t.style.left = Math.min(e.clientX + 12, innerWidth - 310) + "px"; t.style.top = (e.clientY + 12) + "px"; }
function hideTip() { $("#tip").style.display = "none"; }

// ---------- sidebar ----------
function renderList() {
  const q = $("#filter").value.trim().toLowerCase();
  const items = (state.ov?.targets || []).filter(t => t.name.includes(q));
  $("#list").innerHTML = items.map(t => {
    const h = t.HIGH ?? null, m = t.MEDIUM ?? null;
    const badges = h === null ? "" : `<span class="cnt ${h ? "h" : "z"}">${h}</span><span class="cnt ${m ? "m" : "z"}">${m}</span>`;
    return `<button class="item ${t.name === state.pkg && state.tab === "pkg" ? "on" : ""}" data-name="${esc(t.name)}"><span class="nm">${esc(t.name)}</span>${badges}</button>`;
  }).join("") || `<div class="empty" style="padding:12px 10px">No match. Use Check above for any package.</div>`;
}
$("#list").addEventListener("click", e => { const b = e.target.closest(".item"); if (b) go("pkg", b.dataset.name); });
$("#filter").addEventListener("input", renderList);
$("#checkform").addEventListener("submit", e => {
  e.preventDefault(); const v = $("#checkname").value.trim();
  if (!v) { $("#checkerr").textContent = "Enter a package name"; return; }
  $("#checkerr").textContent = ""; go("pkg", v);
});
$("#checkname").addEventListener("input", () => { $("#checkerr").textContent = ""; });

// ---------- routing via the URL hash, so back/forward and refresh work ----------
function go(tab, pkg) { location.hash = tab === "pkg" ? "pkg=" + encodeURIComponent(pkg || state.pkg || "") : tab; }
window.addEventListener("hashchange", route);
function route() {
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith("pkg=") && h.length > 4) { state.tab = "pkg"; loadPackage(h.slice(4)); }
  else if (h === "map" || h === "scan") { state.tab = h; render(); }
  else { go("pkg", state.ov?.targets?.[0]?.name || "requests"); }
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.tab === state.tab));
  renderList();
}
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => go(b.dataset.tab)));

// ---------- package view ----------
async function loadPackage(name) {
  state.pkg = name.toLowerCase(); state.result = null; state.filter = "ALL"; render();
  try {
    const res = await fetch("/api/check?name=" + encodeURIComponent(name));
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    if (state.pkg === name.toLowerCase()) { state.pkg = data.target; state.result = data; render(); renderList(); }
  } catch (err) { $("#view").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
}

function renderPackage() {
  const v = $("#view");
  if (!state.result) { v.innerHTML = `<h2>${esc(state.pkg)}</h2><p class="sub"><span class="spin"></span>Scanning every PyPI name and pulling metadata for matches…</p>`; return; }
  const { target, rows, names_scanned, match_ms } = state.result;
  const n = lv => rows.filter(r => r.level === lv).length;
  const shown = rows.map((r, i) => [r, i]).filter(([r]) => state.filter === "ALL" || r.level === state.filter);
  v.innerHTML = `
    <h2>${esc(target)}</h2>
    <p class="sub">${rows.length} lookalikes among ${names_scanned.toLocaleString()} PyPI names, matched in ${(match_ms/1000).toFixed(2)}s ·
      <a href="https://pypi.org/project/${esc(target)}/" target="_blank" rel="noopener" style="color:inherit">view on PyPI</a></p>
    ${rows.length ? `<div class="pkg">
      <div class="orbitbox">${orbitSVG(rows, 320, {big:true, label:target})}
        <div class="legend"><span><i style="background:var(--high)"></i>high</span><span><i style="background:var(--medium)"></i>medium</span><span><i style="background:var(--low)"></i>low</span>
        <span>inner ring: lookalike characters · middle: 1 edit · outer: 2 edits</span></div></div>
      <div>
        <div class="chips">${["ALL","HIGH","MEDIUM","LOW"].map(f => `<button class="chip ${state.filter === f ? "on" : ""}" data-f="${f}">${f === "ALL" ? "All" : f[0] + f.slice(1).toLowerCase()} ${f === "ALL" ? rows.length : n(f)}</button>`).join("")}</div>
        <div class="rows">${shown.map(([r, i]) => `
          <div class="row" data-i="${i}">
            <div><a href="https://pypi.org/project/${esc(r.name)}/" target="_blank" rel="noopener">${esc(r.name)}</a><span class="kind">${esc(r.detail)}</span></div>
            <span class="pill ${r.level}">${r.level} ${r.score}</span>
            <div class="meta">${ago(r.meta.first_upload)} · ${r.meta.releases ?? 0} release${r.meta.releases === 1 ? "" : "s"}${r.meta.summary ? " · " + esc(r.meta.summary.slice(0, 90)) : ""}</div>
            <div class="why">${esc(r.reasons.join(" · ") || "no warning signs")}</div>
          </div>`).join("") || `<div class="empty">Nothing at this level.</div>`}</div>
      </div></div>` : `<div class="empty">No lookalike names found. Short or very unusual names often have none.</div>`}`;

  v.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => { state.filter = c.dataset.f; renderPackage(); }));
  // Link dots and rows both ways: hover a dot to see it, click to jump to its row.
  v.querySelectorAll(".orbitbox .dot").forEach(d => {
    const r = rows[d.dataset.i];
    d.addEventListener("mousemove", e => tip(e, `${r.name} · ${r.detail} · ${r.level} ${r.score}`));
    d.addEventListener("mouseleave", hideTip);
    d.addEventListener("click", () => {
      if (state.filter !== "ALL" && r.level !== state.filter) { state.filter = "ALL"; renderPackage(); }
      const row = $(`.row[data-i="${d.dataset.i}"]`);
      row?.scrollIntoView({behavior:"smooth", block:"center"}); row?.classList.add("hot"); setTimeout(() => row?.classList.remove("hot"), 1400);
    });
  });
  v.querySelectorAll(".row").forEach(row => {
    const dot = () => v.querySelector(`.dot[data-i="${row.dataset.i}"]`);
    row.addEventListener("mouseenter", () => dot()?.classList.add("hot"));
    row.addEventListener("mouseleave", () => dot()?.classList.remove("hot"));
  });
}

// ---------- ecosystem map ----------
function renderMap() {
  const v = $("#view"), s = state.ov?.summary;
  if (!s) { v.innerHTML = `<div class="empty">No sweep yet. Run <code>python3 radar.py sweep</code> once, then refresh this page.</div>`; return; }
  const by = {};
  state.ov.rows.forEach(r => (by[r.target] ??= []).push(r));
  const stat = (l, x) => `<div class="stat"><span>${l}</span><b>${x}</b></div>`;
  v.innerHTML = `<div class="stats">
      ${stat("Names scanned", s.names_scanned.toLocaleString())}${stat("Comparisons", s.comparisons.toLocaleString())}
      ${stat("Matching time", (s.matcher_ms/1000).toFixed(1) + "s")}${stat("Lookalikes", s.lookalikes.toLocaleString())}
      ${stat("High risk", s.high)}${stat("New in last 90 days", s.registered_last_90_days)}</div>
    <p class="sub">Sweep of ${s.targets} popular packages from ${esc(s.generated.slice(0,10))}. Click any chart to open that package.</p>
    <div class="grid">${state.ov.targets.filter(t => by[t.name]).map(t => `
      <div class="card" data-name="${esc(t.name)}">${orbitSVG(by[t.name], 160, {label:t.name})}<small>${by[t.name].length} lookalikes · ${t.HIGH} high</small></div>`).join("")}</div>`;
  v.querySelectorAll(".card").forEach(c => {
    const list = by[c.dataset.name];
    c.addEventListener("click", () => go("pkg", c.dataset.name));
    c.querySelectorAll(".dot").forEach(d => {
      const r = list[d.dataset.i];
      d.addEventListener("mousemove", e => tip(e, `${r.name} · ${r.detail} · ${r.level} ${r.score}`));
      d.addEventListener("mouseleave", hideTip);
    });
  });
}

// ---------- requirements scan ----------
const SAMPLE = "numpy>=1.26\npamdas==0.1\nflask\nrequests[socks]\npython-dateutils\n";
function renderScan() {
  const v = $("#view");
  v.innerHTML = `<h2 style="font-family:inherit">Scan requirements</h2>
    <p class="sub">Paste a requirements.txt. Each dependency is checked against the popular packages it might be imitating.</p>
    <textarea id="reqs" spellcheck="false" aria-label="Requirements">${esc(state.scanText ?? SAMPLE)}</textarea>
    <div style="display:flex;gap:8px;margin:10px 0 18px;align-items:center">
      <button class="btn" id="runscan">Scan</button><button class="btn ghost" id="clear">Clear</button><span id="scanerr" class="err" style="font-size:13px"></span></div>
    <div class="rows" id="scanout"></div>`;
  $("#reqs").addEventListener("input", e => { state.scanText = e.target.value; $("#scanerr").textContent = ""; });
  $("#clear").addEventListener("click", () => { state.scanText = ""; state.scanResults = null; renderScan(); });
  $("#runscan").addEventListener("click", runScan);
  if (state.scanResults) showScan();
  else if (state.autoScan) { state.autoScan = false; runScan(); }   // opened from `radar.py scan`
}
async function runScan() {
  const text = $("#reqs").value;
  if (!text.trim()) { $("#scanerr").textContent = "Paste at least one requirement first"; return; }
  $("#scanout").innerHTML = `<p class="sub"><span class="spin"></span>Scanning…</p>`;
  try {
    const res = await fetch("/api/scan", {method:"POST", body:text});
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    state.scanResults = data.results; showScan();
  } catch (err) { $("#scanout").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
}
function showScan() {
  const rs = state.scanResults;
  if (!rs.length) { $("#scanout").innerHTML = `<div class="empty">No package names found in that text.</div>`; return; }
  const order = {MISSING:0, HIGH:1, MEDIUM:2, LOW:3, OK:4};
  $("#scanout").innerHTML = [...rs].sort((a, b) => order[a.status] - order[b.status]).map(r => `
    <div class="row"><div><a href="https://pypi.org/project/${esc(r.name)}/" target="_blank" rel="noopener">${esc(r.name)}</a>
      ${r.target ? `<span class="kind">${esc(r.detail)} of <b>${esc(r.target)}</b></span>` : ""}</div>
      <span class="pill ${r.status}">${r.status}${r.score !== undefined ? " " + r.score : ""}</span>
      ${r.reasons ? `<div class="why">${esc(r.reasons.join(" · "))}</div>` : ""}</div>`).join("");
}

function render() {
  hideTip();
  if (state.tab === "pkg") renderPackage(); else if (state.tab === "map") renderMap(); else renderScan();
}

(async () => {
  try { state.ov = await (await fetch("/api/overview")).json(); }
  catch { state.ov = {targets:[], rows:[], summary:null}; }
  const s = state.ov.summary;
  $("#brandsub").textContent = s ? `${s.names_scanned.toLocaleString()} names · last sweep ${s.generated.slice(0,10)}` : "Run a sweep to fill in counts";
  if (state.ov.scan) { state.scanText = state.ov.scan; state.autoScan = true; }
  route();
})();
</script></body></html>
"""


def main():
    if not RESULTS.exists():
        sys.exit("no results yet: run `python3 radar.py sweep` first")
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "report.html"
    out.write_text(build(json.loads(RESULTS.read_text(encoding="utf-8"))), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

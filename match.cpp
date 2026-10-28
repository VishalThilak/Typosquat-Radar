// match.cpp - the fast part of Typosquat Radar.
//
// Given every package name on PyPI and a list of popular "target" packages,
// find names that are suspiciously close to a target and label HOW they are
// close (swapped letters, keyboard slip, lookalike characters, ...).
//
// Build:  g++ -O2 -std=c++17 -pthread match.cpp -o match
// Usage:  ./match names.txt targets.txt [threads]  > candidates.tsv
//
// Both input files hold one already-normalized name per line (lowercase,
// runs of "-", "_", "." collapsed to "-"; radar.py does that normalization).
// Output is tab-separated: target, candidate, distance, kind, detail.
// A one-line timing summary is printed to stderr.

#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

using std::string;
using std::vector;

// ---------------------------------------------------------------------------
// Edit distance (optimal string alignment variant of Damerau-Levenshtein).
// Counts insertions, deletions, substitutions, AND swaps of two neighbouring
// letters as one edit each, because "reqeusts" is one typo, not two.
// Returns limit + 1 as soon as the answer is known to exceed `limit`, which
// is what keeps 100 targets x 900k names fast.
// ---------------------------------------------------------------------------
// Scratch rows for osa_distance(). Each worker thread owns one and passes it
// in, so the ~20 million calls in a sweep reuse memory instead of allocating.
// (An earlier version used thread_local vectors; MinGW on Windows can corrupt
// the heap when such objects are destroyed at thread exit, so this is explicit.)
struct DistanceScratch {
    vector<int> prev2, prev, cur;
};

int osa_distance(const string& a, const string& b, int limit, DistanceScratch& w) {
    const int n = static_cast<int>(a.size());
    const int m = static_cast<int>(b.size());
    if (std::abs(n - m) > limit) return limit + 1;   // length gap alone is too big

    // Three rolling rows of the DP table: two rows back (for swaps), previous, current.
    vector<int>& prev2 = w.prev2;
    vector<int>& prev = w.prev;
    vector<int>& cur = w.cur;
    prev2.assign(m + 1, 0); prev.assign(m + 1, 0); cur.assign(m + 1, 0);
    for (int j = 0; j <= m; ++j) prev[j] = j;         // turning "" into b[0..j) takes j inserts

    for (int i = 1; i <= n; ++i) {
        cur[0] = i;                                   // turning a[0..i) into "" takes i deletes
        int row_min = cur[0];
        for (int j = 1; j <= m; ++j) {
            int cost = (a[i - 1] == b[j - 1]) ? 0 : 1;
            int best = std::min({prev[j] + 1,          // delete a[i-1]
                                 cur[j - 1] + 1,       // insert b[j-1]
                                 prev[j - 1] + cost}); // substitute (or match)
            // Swap of two neighbouring letters, e.g. "ue" <-> "eu".
            if (i > 1 && j > 1 && a[i - 1] == b[j - 2] && a[i - 2] == b[j - 1])
                best = std::min(best, prev2[j - 2] + 1);
            cur[j] = best;
            row_min = std::min(row_min, best);
        }
        if (row_min > limit) return limit + 1;        // every path is already too expensive
        std::swap(prev2, prev);
        std::swap(prev, cur);
    }
    return prev[m];
}

// ---------------------------------------------------------------------------
// "Skeleton" of a name: replace characters that look alike with one canonical
// character. Two different names with the same skeleton look the same to a
// human skimming a requirements file (e.g. "djang0" and "django").
// ---------------------------------------------------------------------------
string skeleton(const string& s) {
    string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size(); ++i) {
        char c = s[i];
        // Two-letter lookalikes first: "rn" reads as "m", "vv" reads as "w".
        if (c == 'r' && i + 1 < s.size() && s[i + 1] == 'n') { out += 'm'; ++i; continue; }
        if (c == 'v' && i + 1 < s.size() && s[i + 1] == 'v') { out += 'w'; ++i; continue; }
        switch (c) {
            case '0': out += 'o'; break;
            case '1': case 'i': case 'l': out += 'l'; break;
            case '5': out += 's'; break;
            case '3': out += 'e'; break;
            default: out += c;
        }
    }
    return out;
}

// QWERTY keyboard layout, used to spot "fat finger" substitutions.
bool keyboard_adjacent(char x, char y) {
    static const char* rows[] = {"1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"};
    int rx = -1, cx = -1, ry = -1, cy = -1;
    for (int r = 0; r < 4; ++r)
        for (int c = 0; rows[r][c]; ++c) {
            if (rows[r][c] == x) { rx = r; cx = c; }
            if (rows[r][c] == y) { ry = r; cy = c; }
        }
    if (rx < 0 || ry < 0) return false;               // not a letter or digit key
    // Neighbouring keys: same row and one column apart, or one row apart and
    // at most one column apart (rows are offset, so this is a slight overcount).
    return std::abs(rx - ry) <= 1 && std::abs(cx - cy) <= 1 && !(rx == ry && cx == cy);
}

bool lookalike_char(char x, char y) {
    // Single characters that are easy to misread for each other.
    static const char* pairs[] = {"0o", "1l", "1i", "il", "5s", "3e"};
    for (const char* p : pairs)
        if ((p[0] == x && p[1] == y) || (p[0] == y && p[1] == x)) return true;
    return false;
}

// Describe a distance-1 difference between candidate `c` and target `t`.
// Returns {kind, detail}; kind is a short machine-friendly label.
std::pair<string, string> classify_one_edit(const string& c, const string& t) {
    size_t i = 0;
    while (i < c.size() && i < t.size() && c[i] == t[i]) ++i;   // first position that differs

    if (c.size() == t.size()) {
        // Either two neighbouring letters were swapped, or one letter changed.
        if (i + 1 < c.size() && c[i] == t[i + 1] && c[i + 1] == t[i])
            return {"swap", string("letters swapped (") + t[i] + "/" + t[i + 1] + ")"};
        char want = t[i], got = c[i];
        if (lookalike_char(want, got))
            return {"lookalike", string("lookalike character (") + want + " -> " + got + ")"};
        if (keyboard_adjacent(want, got))
            return {"keyboard", string("keyboard slip (") + want + " -> " + got + ")"};
        return {"substitution", string("letter changed (") + want + " -> " + got + ")"};
    }
    if (c.size() == t.size() + 1) {
        char extra = c[i];
        if (extra == '-') return {"separator", "extra separator"};
        if ((i > 0 && c[i - 1] == extra) || (i + 1 < c.size() && c[i + 1] == extra))
            return {"double", string("doubled letter (") + extra + ")"};
        return {"insertion", string("extra letter (") + extra + ")"};
    }
    // Candidate is one character shorter than the target.
    char missing = t[i];
    if (missing == '-') return {"separator", "separator dropped"};
    return {"omission", string("missing letter (") + missing + ")"};
}

// Longer names can absorb two typos and still fool people; short ones can't
// (two edits away from "six" is half of PyPI).
int max_distance_for(const string& target) { return target.size() >= 10 ? 2 : 1; }

vector<string> read_lines(const char* path) {
    std::ifstream in(path);
    if (!in) { std::cerr << "cannot open " << path << "\n"; std::exit(1); }
    vector<string> lines;
    string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();   // Windows line endings
        if (!line.empty()) lines.push_back(line);
    }
    return lines;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        std::cerr << "usage: match names.txt targets.txt [threads]\n";
        return 1;
    }
    const auto t_start = std::chrono::steady_clock::now();

    const vector<string> names = read_lines(argv[1]);
    const vector<string> targets = read_lines(argv[2]);
    unsigned threads = argc > 3 ? std::stoul(argv[3]) : std::thread::hardware_concurrency();
    if (threads == 0) threads = 1;

    // Bucket names by length so each target only scans names of similar length.
    size_t max_len = 0;
    for (const auto& n : names) max_len = std::max(max_len, n.size());
    vector<vector<int>> by_length(max_len + 1);
    for (int i = 0; i < static_cast<int>(names.size()); ++i) by_length[names[i].size()].push_back(i);

    // Index every name by its skeleton so lookalike-character squats are a hash lookup.
    std::unordered_map<string, vector<int>> by_skeleton;
    by_skeleton.reserve(names.size());
    for (int i = 0; i < static_cast<int>(names.size()); ++i) by_skeleton[skeleton(names[i])].push_back(i);

    // Each worker thread handles every `threads`-th target and writes into its
    // own buffer, so no locking is needed. Buffers are printed in order at the end.
    vector<std::ostringstream> out(threads);
    vector<long long> comparisons(threads, 0);

    auto worker = [&](unsigned id) {
        DistanceScratch scratch;                                  // this thread's DP rows
        for (size_t ti = id; ti < targets.size(); ti += threads) {
            const string& t = targets[ti];
            const int limit = max_distance_for(t);
            vector<int> found;

            // 1) Edit-distance neighbours.
            size_t lo = t.size() > static_cast<size_t>(limit) ? t.size() - limit : 0;
            size_t hi = std::min(max_len, t.size() + limit);
            for (size_t len = lo; len <= hi; ++len) {
                for (int idx : by_length[len]) {
                    ++comparisons[id];
                    const string& c = names[idx];
                    if (c == t) continue;                       // the real package
                    int d = osa_distance(c, t, limit, scratch);
                    if (d > limit) continue;
                    found.push_back(idx);
                    if (d == 1) {
                        auto [kind, detail] = classify_one_edit(c, t);
                        out[id] << t << '\t' << c << '\t' << d << '\t' << kind << '\t' << detail << '\n';
                    } else {
                        out[id] << t << '\t' << c << '\t' << d << '\t' << "two-edits" << '\t' << "two edits away" << '\n';
                    }
                }
            }

            // 2) Lookalike-skeleton matches the distance pass didn't already report
            //    (e.g. "rnatplotlib" vs "matplotlib" is distance 2 on a short-ish name).
            std::sort(found.begin(), found.end());
            auto it = by_skeleton.find(skeleton(t));
            if (it != by_skeleton.end()) {
                for (int idx : it->second) {
                    const string& c = names[idx];
                    if (c == t || std::binary_search(found.begin(), found.end(), idx)) continue;
                    out[id] << t << '\t' << c << '\t' << 0 << '\t' << "lookalike" << '\t'
                            << "reads the same as " << t << '\n';
                }
            }
        }
    };

    // Everything above is loading and indexing; time the matching on its own too.
    const auto t_match = std::chrono::steady_clock::now();
    vector<std::thread> pool;
    for (unsigned i = 0; i < threads; ++i) pool.emplace_back(worker, i);
    for (auto& th : pool) th.join();

    long long total_cmp = 0;
    for (unsigned i = 0; i < threads; ++i) {
        std::cout << out[i].str();
        total_cmp += comparisons[i];
    }

    const auto t_end = std::chrono::steady_clock::now();
    const double ms = std::chrono::duration<double, std::milli>(t_end - t_start).count();
    const double match_ms = std::chrono::duration<double, std::milli>(t_end - t_match).count();
    std::cerr << "names=" << names.size() << " targets=" << targets.size()
              << " comparisons=" << total_cmp << " threads=" << threads
              << " ms=" << static_cast<long long>(ms)
              << " match_ms=" << static_cast<long long>(match_ms) << "\n";
    return 0;
}

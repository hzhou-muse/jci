"""Find the lines in a console log that explain a failure.

Rules are tried in order; a line is reported under the first rule it
matches. Shell trace lines (`+ ...`) are skipped: they echo script text, not
results.
"""

import re

RULES = [
    # most telling first: summaries keep the top-ranked hits
    ("compile-error", r"^\S+:\d+(:\d+)?: (fatal )?error: "),
    ("link-error", r"undefined reference to |^/usr/bin/ld: |collect2: error: "),
    ("configure-error", r"^configure: error: "),
    ("sanitizer", r"^==\d+==ERROR: |: runtime error: "),
    ("crash", r"Segmentation fault|SIGSEGV|core dumped|Assertion .* failed"),
    ("merge-conflict", r"^CONFLICT \(|^Could not apply "),
    ("test-failed", r"^not ok \d+ |^Unexpected output in \S+:|^Program \S+ exited without No Errors"
                    r"|^\d+ tests failed out of \d+"),
    ("timeout", r"TIMED OUT|[Tt]imed out after|Build timed out"),
    ("error", r"^(fatal )?error: |^ERROR: |^FATAL: "),
    ("aborted", r"^Aborted by |^Build was aborted"),
    ("jenkins", r"^Build step '.*' (marked|changed) build (as|result to) "),
    ("make", r"^g?make(\[\d+\])?: \*\*\* "),
    ("missing-command", r": command not found$"),
]


def compile_rules():
    return [(n, re.compile(r)) for n, r in RULES]


def top(hits, rules, n):
    """The n most telling hits (by rule rank, then position), in log order."""
    rank = {name: i for i, (name, _) in enumerate(rules)}
    best = sorted(hits, key=lambda h: (rank.get(h[1], len(rank)), h[0]))[:n]
    return sorted(best)


def signature(hits):
    """What a failure looks like, ignoring numbers, so cells failing the same
    way compare equal even when line numbers or Makefile lines differ."""
    return frozenset(re.sub(r"\d+", "N", h[2].strip()) for h in hits)


def find(lines, rules):
    """Return [(index, rule, text)] for lines matching a rule, deduplicated by text."""
    hits = []
    seen = set()
    for i, line in enumerate(lines):
        if line.startswith("+ ") or line.startswith("++ "):
            continue
        for name, rx in rules:
            if rx.search(line):
                key = line.strip()
                if key not in seen:
                    seen.add(key)
                    hits.append((i, name, line))
                break
    return hits


def blocks(indices, n_lines, context):
    """Merge [i-context, i+context] windows into sorted, non-overlapping (start, end) ranges."""
    out = []
    for i in sorted(indices):
        a, b = max(0, i - context), min(n_lines - 1, i + context)
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def test_output(trace, details=""):
    """The useful part of a failed test's text: MPICH's runtests puts the
    program output after '## Test output', prefixed with '## '."""
    lines = (trace or details or "").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## Test output"):
            body = [l[3:] if l.startswith("## ") else l.lstrip("#") for l in lines[i + 1:]]
            return [l for l in body if l.strip()]
    return [l for l in lines if l.strip()]

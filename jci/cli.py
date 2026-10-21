"""jci command line: read-only triage of MPICH PR builds on Jenkins."""

import argparse
import os
import re
import sys

from . import __version__, config, extract, model, render
from .cache import Cache
from .errors import EXIT_FAILED, EXIT_OK, JciError, NotFoundError, UsageError
from .http import GitHubClient, JenkinsClient

FAILED_STATES = ("FAILURE", "UNSTABLE", "ABORTED")

EPILOG = """\
triage path:
  jci pr 7123                          builds for the PR's head commit
  jci matrix JOB#N --failed            failed cells of a matrix build
  jci failures JOB#N                   failed tests, grouped, first error lines
  jci log JOB#N --cell asan --grep session_inter -C 20

refs: JOB#N | JOB (last build) | pr:N | pr:N/JOB | a pasted build URL
--cell: a cell label as printed (noerrorchecking/ch4-ofi), axis values (asan),
        or key=value pairs; unique prefixes work
exit: 0 ok/running, 1 build failed, 2 usage, 3 unreachable/auth, 4 not found
env: JENKINS_HOST (default jenkins-pmrs.cels.anl.gov), JENKINS_USER + JENKINS_TOKEN,
     GITHUB_TOKEN (else `gh auth token`)
"""


class Ctx:
    def __init__(self, args):
        self.args = args
        self.cfg = config.Config()
        cache = None if args.no_cache else Cache(self.cfg.cache_dir, self.cfg.cache_max_bytes)
        self.jenkins = JenkinsClient(self.cfg, cache)
        self.github = GitHubClient(self.cfg)
        if args.repo:
            self.github.repo = args.repo
        self.out = render.Out(args.max_lines or self.cfg.max_lines,
                              args.max_bytes or self.cfg.max_bytes)


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        ctx = Ctx(args)
        return args.func(ctx, args)
    except JciError as e:
        sys.stderr.write("jci: %s\n" % e)
        return e.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--max-lines", type=int, metavar="N", help="output line budget (default 200)")
    common.add_argument("--max-bytes", type=int, metavar="N", help="output byte budget (default 16384)")
    common.add_argument("--no-cache", action="store_true", help="bypass the finished-build cache")
    common.add_argument("--repo", metavar="OWNER/NAME", help="GitHub repo for pr refs (default pmodels/mpich)")

    p = argparse.ArgumentParser(prog="jci", description=__doc__, epilog=EPILOG,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="jci " + __version__)
    sub = p.add_subparsers(metavar="COMMAND")

    def add(name, func, help, example):
        sp = sub.add_parser(name, parents=[common], help=help, description=help,
                            epilog="example: " + example,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(func=func)
        return sp

    sp = add("pr", cmd_pr, "Jenkins builds for a PR's head commit and how each did", "jci pr 7123")
    sp.add_argument("pr", type=int)

    sp = add("build", cmd_build, "one build: result, duration, cause, parameters, tests",
             "jci build mpich-review-ch4-ofi#5654")
    sp.add_argument("ref")

    sp = add("matrix", cmd_matrix, "the cells of a matrix build", "jci matrix pr:7123 --failed")
    sp.add_argument("ref")
    sp.add_argument("--failed", action="store_true", help="only cells that did not succeed")

    sp = add("failures", cmd_failures, "failed tests grouped by name, with their first error lines",
             "jci failures mpich-review-ch4-ofi#5654 --cell asan")
    sp.add_argument("ref")
    sp.add_argument("--cell", metavar="AXES")
    sp.add_argument("--lines", type=int, default=20, metavar="N", help="error lines per test (default 20)")

    sp = add("log", cmd_log, "a bounded slice of a console log, with original line numbers",
             "jci log mpich-review-ch4-ofi#5646 --cell default --errors")
    sp.add_argument("ref")
    sp.add_argument("--cell", metavar="AXES")
    m = sp.add_mutually_exclusive_group()
    m.add_argument("--errors", action="store_true", help="lines matching the failure rules (default for a failed build)")
    m.add_argument("--grep", metavar="RE", help="lines matching a Python regex")
    m.add_argument("--tail", type=int, metavar="N", help="last N lines (default 50 for a passing build)")
    m.add_argument("--range", metavar="A:B", help="lines A to B, 1-based, inclusive")
    sp.add_argument("-C", "--context", type=int, metavar="N", help="context lines for --errors/--grep (default 3)")

    sp = add("artifacts", cmd_artifacts, "files archived by a build; --get saves one locally",
             "jci artifacts mpich-review-ch4-ofi#5654 --cell asan --get config.log")
    sp.add_argument("ref")
    sp.add_argument("--cell", metavar="AXES")
    sp.add_argument("--get", metavar="PATH", help="download this artifact and print its local path")
    sp.add_argument("--out", metavar="DIR", help="download directory (default under the cache dir)")
    return p


# ---- pr -------------------------------------------------------------------

def pr_builds(ctx, pr):
    """[(status, Build or None)] for the Jenkins statuses on the PR's head commit."""
    sha = ctx.github.pr_head(pr)
    statuses = [s for s in ctx.github.statuses(sha)
                if _host(s.get("target_url") or "") in ctx.jenkins.hosts]

    def load(s):
        try:
            return model.fetch_build(ctx.jenkins, model.parse_ref(s["target_url"], ctx.jenkins))
        except NotFoundError:
            return None

    builds = model.parallel(load, statuses)
    rows = list(zip(statuses, builds))
    rows.sort(key=lambda r: (_verdict(*r) == "PASS", r[0]["context"]))
    return sha, rows


def cmd_pr(ctx, args):
    sha, rows = pr_builds(ctx, args.pr)
    if not rows:
        raise NotFoundError("PR #%d head %s has no Jenkins statuses" % (args.pr, sha[:7]))
    model.parallel(lambda r: r[1] and r[1].cells and r[1].load_test_counts(), rows)
    items = [_pr_item(s, b) for s, b in rows]
    failed = any(i["verdict"] == "FAIL" for i in items)
    if args.json:
        render.emit_json("pr", {"pr": args.pr, "repo": ctx.github.repo, "head": sha, "builds": items})
        return EXIT_FAILED if failed else EXIT_OK
    out = ctx.out
    out.add("PR #%d head %s  (%d Jenkins statuses)" % (args.pr, sha[:7], len(items)))
    w = max(len(i["build"] or i["context"]) for i in items)
    for i in items:
        ctxname = "" if i["job"] == i["context"] or i["job"].endswith("-" + i["context"]) else "  [%s]" % i["context"]
        out.add("  %-4s  %-*s  %s%s" % (i["verdict"], w, i["build"] or i["context"], i["summary"], ctxname))
    first = next((i for i in items if i["verdict"] == "FAIL" and not i.get("pruned")), None)
    if first:
        out.next("jci failures %s" % first["build"] if first["kind"] == "matrix"
                 else "jci log %s --errors" % first["build"])
    sys.stdout.write(out.text())
    return EXIT_FAILED if failed else EXIT_OK


def _verdict(status, build):
    if build is None:
        # pruned from Jenkins; fall back to what GitHub recorded
        return {"success": "PASS", "pending": "RUN"}.get(status["state"], "FAIL")
    if build.state == "RUNNING":
        return "RUN"
    return "PASS" if build.state == "SUCCESS" else "FAIL"


def _pr_item(status, b):
    item = {"context": status["context"], "github_state": status["state"],
            "verdict": _verdict(status, b), "url": status["target_url"]}
    if b is None:
        ref = model.parse_ref(status["target_url"], _NoHostCheck())
        item.update(job=ref.job, build=str(ref), kind=None, result=None, pruned=True,
                    summary="pruned from Jenkins; GitHub: %s" % status.get("description", ""))
        return item
    item.update(job=b.ref.job, build=b.name, kind=b.kind, result=b.state,
                duration_ms=b.duration, summary=_summary(b))
    if b.cells:
        item["cells"] = len(b.cells)
        item["failed_cells"] = [c.label for c in b.failed_cells()]
    item["failed_tests"] = b.fail_count
    item["total_tests"] = b.total_count
    return item


def _summary(b):
    parts = [b.state]
    if b.state != "RUNNING":
        parts[0] += " " + render.duration(b.duration)
    if b.cells:
        nf = len(b.failed_cells())
        parts.append("%d/%d cells failed" % (nf, len(b.cells)) if nf else "%d cells" % len(b.cells))
    if b.fail_count:
        parts.append("%d/%d tests failed" % (b.fail_count, b.total_count))
    return ", ".join(parts)


class _NoHostCheck:
    def path_of(self, url):
        return "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url


# ---- refs -----------------------------------------------------------------

def resolve(ctx, text):
    """Resolve any ref, including pr:N[/JOB], to a Build."""
    m = re.match(r"^pr:(\d+)(?:/(.+))?$", text)
    if not m:
        return model.fetch_build(ctx.jenkins, model.parse_ref(text, ctx.jenkins))
    pr, want = int(m.group(1)), m.group(2)
    _, rows = pr_builds(ctx, pr)
    rows = [(s, b) for s, b in rows if b is not None]
    if want:
        rows = [(s, b) for s, b in rows if want in (b.ref.job, s["context"])] or \
               [(s, b) for s, b in rows if want in b.ref.job or want in s["context"]]
    if not rows:
        raise NotFoundError("pr:%d has no Jenkins build%s" % (pr, " matching " + want if want else ""))
    if len(rows) == 1:
        return rows[0][1]
    failed = [(s, b) for s, b in rows if b.state in FAILED_STATES]
    if len(failed) == 1:
        return failed[0][1]
    pick = failed or rows
    raise UsageError("%s names %d builds; pick one: %s"
                     % (text, len(pick), ", ".join(b.name for _, b in pick)))


def pick_cell(build, spec, purpose):
    """The cell a log/artifacts command reads. Without --cell on a matrix
    build, use the only failed cell, else ask for one."""
    cell = model.resolve_target(build, spec)
    if cell or not build.cells:
        return cell, None
    failed = build.failed_cells()
    if len(failed) == 1:
        return failed[0], "the only failed cell"
    if not failed:
        return None, "matrix build log; add --cell for a cell's %s" % purpose
    raise UsageError("%s has %d failed cells; add --cell with one of: %s"
                     % (build.name, len(failed), ", ".join(c.label for c in failed)))


def exit_for(state):
    return EXIT_FAILED if state in FAILED_STATES else EXIT_OK


# ---- build ----------------------------------------------------------------

def cmd_build(ctx, args):
    b = resolve(ctx, args.ref)
    out = ctx.out
    if args.json:
        render.emit_json("build", {
            "build": b.name, "job": b.ref.job, "number": b.number, "kind": b.kind,
            "result": b.state, "duration_ms": b.duration, "timestamp_ms": b.timestamp,
            "url": ctx.jenkins.base + b.url_path, "causes": b.causes, "parameters": b.params,
            "failed_tests": b.fail_count, "total_tests": b.total_count,
            "cells": len(b.cells), "failed_cells": [c.label for c in b.failed_cells()],
            "artifacts": len(b.artifacts)})
        return exit_for(b.state)
    head = "%s  %s" % (b.name, b.state)
    if b.cells:
        nf = len(b.failed_cells())
        head += "  matrix, %d cells%s" % (len(b.cells), " (%d failed)" % nf if nf else "")
    out.add(head)
    out.add("  url:      %s" % (ctx.jenkins.base + b.url_path))
    out.add("  started:  %s   duration: %s" % (render.when(b.timestamp), render.duration(b.duration)))
    for c in b.causes:
        out.add("  cause:    %s" % c)
    for k in sorted(b.params):
        out.add("  param:    %s=%s" % (k, b.params[k]))
    if b.total_count is not None:
        out.add("  tests:    %d failed / %d" % (b.fail_count or 0, b.total_count))
    if b.artifacts:
        out.add("  artifacts: %d" % len(b.artifacts))
    if b.state in FAILED_STATES:
        out.next("jci matrix %s --failed" % b.name if b.cells else "jci log %s --errors" % b.name)
    sys.stdout.write(out.text())
    return exit_for(b.state)


# ---- matrix ---------------------------------------------------------------

def cmd_matrix(ctx, args):
    b = resolve(ctx, args.ref)
    if not b.cells:
        raise UsageError("%s is a %s build, not a matrix; try: jci log %s --errors" % (b.name, b.kind, b.name))
    b.load_test_counts()
    cells = b.failed_cells() if args.failed else b.cells
    if args.json:
        render.emit_json("matrix", {"build": b.name, "result": b.state, "cells": [
            {"label": c.label, "name": c.name, "axes": c.axes, "result": c.state,
             "duration_ms": c.duration, "failed_tests": c.fail_count, "total_tests": c.total_count}
            for c in cells]})
        return exit_for(b.state)
    out = ctx.out
    out.add("%s  %s  %d/%d cells failed" % (b.name, b.state, len(b.failed_cells()), len(b.cells)))
    w = max([len(c.label) for c in cells] + [4])
    for c in cells:
        tests = ""
        if c.total_count is not None:
            tests = "%d/%d tests failed" % (c.fail_count, c.total_count) if c.fail_count else "%d tests" % c.total_count
        elif c.state == "FAILURE":
            tests = "no test report (build failed)"
        out.add("  %-8s  %-*s  %7s  %s" % (c.state, w, c.label, render.duration(c.duration), tests))
    if b.failed_cells():
        out.next("jci failures %s" % b.name)
    sys.stdout.write(out.text())
    return exit_for(b.state)


# ---- failures -------------------------------------------------------------

def cmd_failures(ctx, args):
    b = resolve(ctx, args.ref)
    rules = extract.compile_rules()
    if b.cells:
        spec = args.cell or b.ref.cell
        targets = [b.find_cell(spec)] if spec else b.failed_cells()
    else:
        targets = [None]
    results = model.parallel(lambda c: (c, model.cell_failures(ctx.jenkins, b, c)), targets)

    groups = {}        # (class, test) -> {"cells": [], "lines": [...]}
    no_report = []     # [labels, cell, n_lines, hits] for failed targets without failed test cases
    for cell, cases in results:
        label = cell.label if cell else b.name
        state = cell.state if cell else b.state
        for case in cases or []:
            key = (case["class"], _test_name(case["name"]))
            g = groups.setdefault(key, {"class": case["class"], "test": key[1], "cells": [],
                                        "status": case["status"], "runtests": _is_runtests(case["name"]),
                                        "lines": extract.test_output(case["trace"], case["details"])})
            g["cells"].append(label)
        if not cases and state in FAILED_STATES:
            lines = model.console(ctx.jenkins, b, cell).splitlines()
            hits = extract.find(lines, rules)
            sig = extract.signature(hits)
            same = next((r for r in no_report if extract.signature(r[3]) == sig), None)
            if same:
                same[0].append(label)   # cells failing the same way are shown once
            else:
                no_report.append([[label], cell, len(lines), hits])

    ordered = sorted(groups.values(), key=lambda g: (-len(g["cells"]), g["class"] or "", g["test"]))
    if args.json:
        render.emit_json("failures", {
            "build": b.name, "result": b.state,
            "tests": [dict(g, lines=g["lines"][:args.lines], more_lines=max(0, len(g["lines"]) - args.lines))
                      for g in ordered],
            "log_errors": [{"cells": labels, "errors": [{"line": i + 1, "rule": r, "text": t} for i, r, t in hits]}
                           for labels, _, _, hits in no_report]})
        return exit_for(b.state)

    out = ctx.out
    where = "%d of %d cells" % (len(targets), len(b.cells)) if b.cells else "the build"
    out.add("%s  %s: %d failed test%s in %s" % (b.name, b.state, len(ordered),
                                                "" if len(ordered) == 1 else "s", where))
    for g in ordered:
        name = g["test"] if not g["class"] or g["test"].startswith(g["class"]) else "%s: %s" % (g["class"], g["test"])
        out.add("")
        out.add("  %s  [%s]  cells: %s" % (name, g["status"], ", ".join(g["cells"])))
        for line in g["lines"][:args.lines]:
            out.add("    " + line)
        if len(g["lines"]) > args.lines:
            out.add("    … %d more lines; use --lines %d" % (len(g["lines"]) - args.lines, len(g["lines"])))
    for labels, cell, n_lines, hits in no_report:
        out.add("")
        who = ", ".join(labels) + ": " if cell else ""
        out.add("  %sno failed tests reported; first log errors (of %d lines):" % (who, n_lines))
        if not hits:
            out.add("    (no line matched the error rules; see the log tail)")
        for i, rule, text in extract.top(hits, rules, 8):
            out.add("    %6d  %-15s %s" % (i + 1, rule, text.strip()))
        if len(hits) > 8:
            out.add("    … %d more matches; see jci log --errors" % (len(hits) - 8))
    if ordered:
        g = ordered[0]
        cellopt = " --cell %s" % g["cells"][0] if b.cells else ""
        if g["runtests"]:
            out.next("jci log %s%s --grep '%s' -C 20" % (b.name, cellopt, re.escape(_program(g["test"]))))
        else:
            out.next("jci log %s%s --errors" % (b.name, cellopt))
    elif no_report:
        labels, cell = no_report[0][0], no_report[0][1]
        out.next("jci log %s%s --errors" % (b.name, " --cell %s" % labels[0] if cell else ""))
    sys.stdout.write(out.text())
    return exit_for(b.state)


def _test_name(name):
    # MPICH runtests numbers tests per run ("03087 - ./session/session_inter 5");
    # the number differs between cells, so group without it
    return re.sub(r"^\d+ - (\./)?", "", name or "")


def _is_runtests(name):
    return bool(re.match(r"^\d+ - \./", name or ""))


def _program(test):
    first = test.split()[0] if test.split() else test
    return os.path.basename(first)


# ---- log ------------------------------------------------------------------

def cmd_log(ctx, args):
    b = resolve(ctx, args.ref)
    cell, note = pick_cell(b, args.cell, "log")
    state = cell.state if cell else b.state
    lines = model.console(ctx.jenkins, b, cell).splitlines()
    n = len(lines)
    ctxlines = 3 if args.context is None else args.context

    mode = None
    if args.range:
        m = re.match(r"^(\d+)?:(\d+)?$", args.range)
        if not m:
            raise UsageError("--range takes A:B, e.g. 1:200 or 5000:")
        a = max(1, int(m.group(1) or 1))
        z = min(n, int(m.group(2) or n))
        idx = list(range(a - 1, z))
        mode = "range %d:%d" % (a, z)
    elif args.tail is not None or (not args.grep and not args.errors and state not in FAILED_STATES):
        t = args.tail if args.tail is not None else 50
        idx = list(range(max(0, n - t), n))
        mode = "tail %d" % t
    elif args.grep:
        try:
            rx = re.compile(args.grep)
        except re.error as e:
            raise UsageError("--grep: %s" % e)
        hits = [i for i, l in enumerate(lines) if rx.search(l)]
        idx = _expand(hits, n, ctxlines)
        mode = "grep %r: %d matching lines" % (args.grep, len(hits))
    else:
        hits = extract.find(lines, extract.compile_rules())
        idx = _expand([h[0] for h in hits], n, ctxlines)
        mode = "errors: %d matching lines" % len(hits)

    target = "%s%s" % (b.name, " cell " + cell.label if cell else "")
    if args.json:
        cap = ctx.out.max_lines
        render.emit_json("log", {"build": b.name, "cell": cell.label if cell else None, "result": state,
                                 "total_lines": n, "mode": mode,
                                 "lines": [{"n": i + 1, "text": lines[i]} for i in idx[:cap]],
                                 "truncated": max(0, len(idx) - cap)})
        return exit_for(state)

    out = ctx.out
    out.add("%s  %s  %d lines; %s" % (target, state, n, mode))
    if note:
        out.add("(%s)" % note)
    header = len(out.lines)
    row_of = []          # out line -> log index, for the truncation hint
    prev = None
    for i in idx:
        if prev is not None and i != prev + 1:
            out.add("--")
            row_of.append(i)
        out.add("%6d  %s" % (i + 1, lines[i]))
        row_of.append(i)
        prev = i
    if not idx:
        out.add("(no lines)")
        if mode.startswith("errors"):
            out.next("jci log %s%s --tail 80" % (b.name, " --cell %s" % cell.label if cell else ""))

    def more(k):
        k -= header
        start = row_of[k] + 1 if 0 <= k < len(row_of) else 1
        return "use --range %d:%d" % (start, min(n, start + 199))

    sys.stdout.write(out.text(more))
    return exit_for(state)


def _expand(hits, n, c):
    out = []
    for a, z in extract.blocks(hits, n, c):
        out.extend(range(a, z + 1))
    return out


# ---- artifacts ------------------------------------------------------------

def cmd_artifacts(ctx, args):
    b = resolve(ctx, args.ref)
    cell, note = pick_cell(b, args.cell, "artifacts")
    if cell:
        data = ctx.jenkins.get_json(cell.url_path, "artifacts[relativePath]", cacheable=b.finished)
        paths = [a["relativePath"] for a in data.get("artifacts", [])]
        base = cell.url_path
    else:
        paths, base = b.artifacts, b.url_path
    state = cell.state if cell else b.state
    target = "%s%s" % (b.name, " cell " + cell.label if cell else "")

    if args.get:
        if args.get not in paths:
            raise NotFoundError("%s has no artifact %r" % (target, args.get))
        data = ctx.jenkins.get_bytes(base + "artifact/" + args.get, cacheable=b.finished)
        outdir = args.out or os.path.join(ctx.cfg.cache_dir, "artifacts", b.ref.job.replace("/", "_"),
                                          str(b.number), cell.name if cell else "_")
        dest = os.path.abspath(os.path.join(outdir, args.get))
        if not dest.startswith(os.path.abspath(outdir) + os.sep):
            raise UsageError("refusing artifact path %r" % args.get)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        if args.json:
            render.emit_json("artifacts", {"build": b.name, "artifact": args.get, "path": dest, "bytes": len(data)})
        else:
            sys.stdout.write("%s  (%d bytes)\n" % (dest, len(data)))
        return exit_for(state)

    if args.json:
        render.emit_json("artifacts", {"build": b.name, "cell": cell.label if cell else None,
                                       "artifacts": sorted(paths)})
        return exit_for(state)
    out = ctx.out
    out.add("%s  %d artifacts" % (target, len(paths)))
    if note:
        out.add("(%s)" % note)
    for p in sorted(paths):
        out.add("  " + p)
    if paths:
        out.next("jci artifacts %s%s --get %s" % (b.name, " --cell %s" % cell.label if cell else "", sorted(paths)[0]))
    sys.stdout.write(out.text())
    return exit_for(state)


def _host(url):
    return url.split("://", 1)[-1].split("/", 1)[0]


if __name__ == "__main__":
    sys.exit(main())

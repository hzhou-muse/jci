"""Build references and the Jenkins build model.

MPICH's layout: a PR's head commit carries one GitHub status per Jenkins
job, and each status links straight to that job's build. Most review jobs
are matrix jobs whose cells (MatrixRuns) do the work; the checkers are
freestyle jobs.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from .errors import NotFoundError, UsageError

BUILD_TREE = ("_class,number,result,building,duration,timestamp,url,fullDisplayName,"
              "actions[parameters[name,value],causes[shortDescription,upstreamProject,upstreamBuild],"
              "failCount,skipCount,totalCount],"
              "runs[number,url,result,duration,building],artifacts[relativePath]")
FAILED_CASE = ("FAILED", "REGRESSION")


class Ref:
    """A parsed build reference: job path, optional build number, optional cell."""

    def __init__(self, job_path, number=None, cell=None):
        self.job_path = job_path      # e.g. /job/mpich-review-ch4-ofi
        self.number = number          # None means lastBuild
        self.cell = cell              # e.g. jenkins_configure=asan,label=ubuntu22.04

    @property
    def job(self):
        return "/".join(s for s in self.job_path.split("/job/") if s)

    def __str__(self):
        return "%s#%s" % (self.job, self.number if self.number is not None else "last")


def parse_ref(text, jenkins):
    """Parse JOB, JOB#N or a build URL. pr:N refs are resolved by the caller."""
    if "://" in text or text.startswith("/"):
        return _parse_url_path(jenkins.path_of(text))
    m = re.match(r"^([A-Za-z0-9_.\-/]+?)(?:#(\d+))?$", text)
    if not m:
        raise UsageError("bad build reference %r; use JOB#N, JOB, pr:N or a build URL" % text)
    job_path = "".join("/job/" + s for s in m.group(1).split("/") if s)
    return Ref(job_path, int(m.group(2)) if m.group(2) else None)


def _parse_url_path(path):
    segs = [s for s in path.split("/") if s]
    job_segs = []
    i = 0
    while i < len(segs):
        if segs[i] == "view" and i + 1 < len(segs) and not job_segs:
            i += 2
        elif segs[i] == "job" and i + 1 < len(segs):
            job_segs.append(segs[i + 1])
            i += 2
        else:
            break
    if not job_segs:
        raise UsageError("not a Jenkins job URL: %s" % path)
    rest = segs[i:]
    cell = None
    if rest and "=" in rest[0]:
        cell = rest.pop(0)
    number = None
    if rest and rest[0].isdigit():
        number = int(rest[0])
    return Ref("".join("/job/" + s for s in job_segs), number, cell)


class Cell:
    def __init__(self, run, build_number):
        self.url_path = None
        self.name = run["name"]
        self.axes = dict(kv.split("=", 1) for kv in self.name.split(",") if "=" in kv)
        self.result = run.get("result")
        self.building = run.get("building", False)
        self.duration = run.get("duration", 0)
        self.number = build_number
        self.label = self.name      # short label, set by Build
        self.fail_count = None
        self.total_count = None

    @property
    def state(self):
        return "RUNNING" if self.building or self.result is None else self.result


class Build:
    def __init__(self, jenkins, ref, data):
        self.jenkins = jenkins
        self.ref = ref
        self.data = data
        self.number = data["number"]
        self.kind = _kind(data.get("_class", ""))
        self.result = data.get("result")
        self.building = bool(data.get("building"))
        self.duration = data.get("duration", 0)
        self.timestamp = data.get("timestamp", 0)
        self.url_path = jenkins.path_of(data["url"])
        self.params = {}
        self.causes = []
        self.fail_count = self.total_count = self.skip_count = None
        for a in data.get("actions", []):
            for p in a.get("parameters", []) or []:
                self.params[p["name"]] = p.get("value")
            for c in a.get("causes", []) or []:
                self.causes.append(c.get("shortDescription", ""))
            if "failCount" in a:
                self.fail_count = a.get("failCount")
                self.total_count = a.get("totalCount")
                self.skip_count = a.get("skipCount")
        self.artifacts = [a["relativePath"] for a in data.get("artifacts", []) or []]
        self.cells = []
        for run in data.get("runs", []) or []:
            # a matrix build lists stale runs of cells it did not run; skip them
            if run.get("number") != self.number:
                continue
            path = jenkins.path_of(run["url"])
            run = dict(run, name=path.rstrip("/").split("/")[-2])
            c = Cell(run, self.number)
            c.url_path = path
            self.cells.append(c)
        self.cells.sort(key=lambda c: c.name)
        _set_labels(self.cells)

    @property
    def state(self):
        return "RUNNING" if self.building or self.result is None else self.result

    @property
    def finished(self):
        return not self.building and self.result is not None

    @property
    def name(self):
        return "%s#%d" % (self.ref.job, self.number)

    def failed_cells(self):
        return [c for c in self.cells if c.state not in ("SUCCESS", "RUNNING", "NOT_BUILT")]

    def find_cell(self, spec):
        """Find one cell by a --cell spec: key=value or bare values, separated by
        ',' or '/' (a cell's label); exact values win over unique prefixes."""
        if not self.cells:
            raise UsageError("%s is not a matrix build; drop --cell" % self.name)
        if spec in [c.name for c in self.cells]:
            return next(c for c in self.cells if c.name == spec)
        parts = [p.strip() for p in re.split(r"[,/]", spec) if p.strip()]
        for exact in (True, False):
            hits = [c for c in self.cells if all(_part_matches(c, p, exact) for p in parts)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise UsageError("--cell %s matches %d cells: %s"
                                 % (spec, len(hits), ", ".join(c.label for c in hits)))
        raise UsageError("--cell %s matches no cell of %s; cells: %s"
                         % (spec, self.name, ", ".join(c.label for c in self.cells)))

    def load_test_counts(self):
        """Fill fail/total counts of each cell from one testReport call."""
        if not self.cells:
            return
        try:
            d = self.jenkins.get_json(self.url_path + "testReport",
                                      "childReports[child[url],result[failCount,passCount,skipCount]]",
                                      cacheable=self.finished)
        except NotFoundError:
            return
        by_path = {c.url_path: c for c in self.cells}
        for r in d.get("childReports", []):
            c = by_path.get(self.jenkins.path_of(r["child"]["url"]))
            if c:
                res = r["result"]
                c.fail_count = res.get("failCount", 0)
                c.total_count = res.get("failCount", 0) + res.get("passCount", 0) + res.get("skipCount", 0)


def _kind(cls):
    if cls.endswith("MatrixBuild"):
        return "matrix"
    if cls.endswith("MatrixRun"):
        return "cell"
    if "WorkflowRun" in cls:
        return "pipeline"
    return "freestyle"


def _part_matches(cell, part, exact):
    if "=" in part:
        k, v = part.split("=", 1)
        vals = [cell.axes[k]] if k in cell.axes else []
    else:
        v = part
        vals = list(cell.axes.values())
    return any(x == v if exact else x.startswith(v) for x in vals)


def _set_labels(cells):
    """Label each cell by the values of the axes that differ across the build."""
    if len(cells) < 2:
        for c in cells:
            c.label = "/".join(c.axes.values()) or c.name
        return
    keys = list(cells[0].axes.keys())
    varying = [k for k in keys if len({c.axes.get(k) for c in cells}) > 1]
    for c in cells:
        c.label = "/".join(c.axes.get(k, "") for k in varying) or c.name


def fetch_build(jenkins, ref):
    """Fetch a build (the matrix build when ref names a cell; see ref.cell)."""
    if ref.number is None:
        data = jenkins.get_json(ref.job_path + "/lastBuild", BUILD_TREE)
        return Build(jenkins, Ref(ref.job_path, data["number"], ref.cell), data)
    path = "%s/%d" % (ref.job_path, ref.number)
    key = path + "/api/json?tree=" + BUILD_TREE
    cache = jenkins.cache
    raw = cache.get(jenkins.hosts[0], key) if cache else None
    if raw is None:
        raw = jenkins.get_bytes(path + "/api/json", "tree=" + BUILD_TREE)
        data = json.loads(raw.decode("utf-8"))
        if cache and not data.get("building") and data.get("result") is not None:
            cache.put(jenkins.hosts[0], key, raw)
    else:
        data = json.loads(raw.decode("utf-8"))
    return Build(jenkins, ref, data)


def resolve_target(build, cell_spec):
    """Return (cell or None) for a command that reads one log or report."""
    spec = cell_spec or build.ref.cell
    if spec:
        return build.find_cell(spec)
    return None


def cell_failures(jenkins, build, cell):
    """Failed test cases of one cell (or of a non-matrix build when cell is None).

    Returns a list of dicts, or None when the build published no test report."""
    path = (cell.url_path if cell else build.url_path) + "testReport"
    try:
        d = jenkins.get_json(path, "suites[name,cases[className,name,status,errorDetails,errorStackTrace]]",
                             cacheable=build.finished)
    except NotFoundError:
        return None
    out = []
    for s in d.get("suites", []):
        for c in s.get("cases", []):
            if c.get("status") in FAILED_CASE:
                out.append({
                    "suite": s.get("name"),
                    "class": c.get("className"),
                    "name": c.get("name"),
                    "status": c.get("status"),
                    "details": c.get("errorDetails") or "",
                    "trace": c.get("errorStackTrace") or "",
                })
    return out


def console(jenkins, build, cell):
    path = (cell.url_path if cell else build.url_path) + "consoleText"
    return jenkins.get_text(path, cacheable=build.finished)


def parallel(fn, items, workers=8):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))

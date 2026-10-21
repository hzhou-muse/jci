"""Offline tests: unit tests, golden output replayed from recorded fixtures,
and output-budget tests. Run with: python3 -m unittest discover -s tests

Set JCI_UPDATE_GOLDEN=1 to rewrite tests/golden/ after an intended change,
and JCI_LIVE=1 to also run one smoke test against the real servers.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from jci import cli, config, extract, http, model, render  # noqa: E402
from jci.errors import NotFoundError, UsageError  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

for var in ("JENKINS_HOST", "JENKINS_USER", "JENKINS_TOKEN"):
    os.environ.pop(var, None)       # replay against the default host, anonymously

with open(os.path.join(HERE, "fixtures.json")) as f:
    FIXTURES = json.load(f)
EXTRA = {}    # per-test responses layered over FIXTURES


class FakeJenkins(http.JenkinsClient):
    def get_bytes(self, path, query="", cacheable=False):
        key = "jenkins " + path + ("?" + query if query else "")
        if key in EXTRA:
            return EXTRA[key].encode("utf-8")
        if key not in FIXTURES:
            raise NotFoundError("Jenkins: not found (%s)" % path)
        return FIXTURES[key].encode("utf-8")


class FakeGitHub(http.GitHubClient):
    def get_json(self, path):
        key = "github " + path
        if key not in FIXTURES:
            raise NotFoundError("GitHub: not found (%s)" % path)
        return json.loads(FIXTURES[key])


def run(argv):
    saved = cli.JenkinsClient, cli.GitHubClient
    cli.JenkinsClient, cli.GitHubClient = FakeJenkins, FakeGitHub
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv + ["--no-cache"])
    finally:
        cli.JenkinsClient, cli.GitHubClient = saved
    return code, out.getvalue(), err.getvalue()


class Golden(unittest.TestCase):
    def test_scenarios(self):
        update = os.environ.get("JCI_UPDATE_GOLDEN")
        gdir = os.path.join(HERE, "golden")
        for name, argv in SCENARIOS:
            with self.subTest(name):
                code, out, err = run(argv)
                got = "$ jci %s\nexit=%d\n--- stdout\n%s--- stderr\n%s" % (" ".join(argv), code, out, err)
                path = os.path.join(gdir, name + ".txt")
                if update:
                    os.makedirs(gdir, exist_ok=True)
                    with open(path, "w") as f:
                        f.write(got)
                    continue
                with open(path) as f:
                    self.assertEqual(f.read(), got)

    def test_deterministic(self):
        for name, argv in SCENARIOS:
            with self.subTest(name):
                self.assertEqual(run(argv), run(argv))


class Budget(unittest.TestCase):
    BIG = "/job/big-job/1/"

    def setUp(self):
        EXTRA["jenkins /job/big-job/1/api/json?tree=" + model.BUILD_TREE] = json.dumps({
            "_class": "hudson.model.FreeStyleBuild", "number": 1, "result": "FAILURE",
            "building": False, "url": "https://jenkins-pmrs.cels.anl.gov/job/big-job/1/"})
        log = []
        for i in range(20000):
            log.append("src/x%d.c:%d:1: error: something broke %d %s" % (i, i, i, "x" * 300)
                       if i % 7 == 0 else "ordinary line %d" % i)
        EXTRA["jenkins /job/big-job/1/consoleText"] = "\n".join(log)

    def tearDown(self):
        EXTRA.clear()

    def check(self, argv, max_lines=200, max_bytes=16384):
        code, out, err = run(argv)
        self.assertLessEqual(len(out.splitlines()), max_lines, argv)
        self.assertLessEqual(len(out.encode("utf-8")), max_bytes, argv)
        return out

    def test_defaults(self):
        for argv in (["log", "big-job#1"], ["log", "big-job#1", "--range", "1:"],
                     ["log", "big-job#1", "--grep", "line"], ["failures", "big-job#1"]):
            out = self.check(argv)
            if argv[0] == "log":
                self.assertIn("more lines; use --range", out)

    def test_overrides(self):
        self.check(["log", "big-job#1", "--range", "1:", "--max-lines", "20"], max_lines=20)
        self.check(["log", "big-job#1", "--range", "1:", "--max-bytes", "2000"], max_bytes=2000)

    def test_range_continues(self):
        out = self.check(["log", "big-job#1", "--range", "1:", "--max-lines", "10"])
        last = [l for l in out.splitlines() if l.strip()[:1].isdigit()][-1]
        n = int(last.split()[0])
        self.assertIn("--range %d:" % (n + 1), out)


class Units(unittest.TestCase):
    def setUp(self):
        self.j = http.JenkinsClient(config.Config())

    def test_parse_ref(self):
        r = model.parse_ref("mpich-review-ch4-ofi#5654", self.j)
        self.assertEqual((r.job_path, r.number, r.cell), ("/job/mpich-review-ch4-ofi", 5654, None))
        r = model.parse_ref("mpich-warnings", self.j)
        self.assertIsNone(r.number)
        r = model.parse_ref("https://jenkins-pmrs.cels.anl.gov/view/mpich-review/job/mpich-review-ch4-ofi/"
                            "jenkins_configure=asan,label=ubuntu22.04/5654/consoleText", self.j)
        self.assertEqual((r.job, r.number, r.cell),
                         ("mpich-review-ch4-ofi", 5654, "jenkins_configure=asan,label=ubuntu22.04"))
        r = model.parse_ref("https://jenkins-pmrs.cels.anl.gov/job/a/job/b/7/", self.j)
        self.assertEqual((r.job, r.number), ("a/b", 7))
        with self.assertRaises(UsageError):
            model.parse_ref("https://example.org/job/a/1/", self.j)
        with self.assertRaises(UsageError):
            model.parse_ref("bad ref;rm", self.j)

    def build(self, names):
        runs = [{"number": 3, "result": "SUCCESS",
                 "url": "https://jenkins-pmrs.cels.anl.gov/job/j/%s/3/" % n} for n in names]
        runs.append({"number": 2, "result": "FAILURE",       # stale run, not part of #3
                     "url": "https://jenkins-pmrs.cels.anl.gov/job/j/netmod=old/2/"})
        return model.Build(self.j, model.Ref("/job/j", 3), {
            "_class": "hudson.matrix.MatrixBuild", "number": 3, "result": "SUCCESS",
            "url": "https://jenkins-pmrs.cels.anl.gov/job/j/3/", "runs": runs})

    def test_cells(self):
        b = self.build(["cfg=default,netmod=ch4-ofi", "cfg=default,netmod=ch4-ofi-ze",
                        "cfg=debug,netmod=ch4-ofi", "cfg=debug,netmod=ch4-ofi-ze"])
        self.assertEqual(len(b.cells), 4)
        self.assertEqual(sorted(c.label for c in b.cells),
                         ["debug/ch4-ofi", "debug/ch4-ofi-ze", "default/ch4-ofi", "default/ch4-ofi-ze"])
        self.assertEqual(b.find_cell("debug/ch4-ofi").name, "cfg=debug,netmod=ch4-ofi")   # exact wins
        self.assertEqual(b.find_cell("netmod=ch4-ofi-ze,cfg=deb").name, "cfg=debug,netmod=ch4-ofi-ze")
        self.assertEqual(b.find_cell("cfg=default,netmod=ch4-ofi").name, "cfg=default,netmod=ch4-ofi")
        with self.assertRaises(UsageError):
            b.find_cell("debug")            # two cells
        with self.assertRaises(UsageError):
            b.find_cell("nosuch")

    def test_extract(self):
        lines = ["+ echo 'error: in a trace'", "a.c:1:2: error: boom", "make: *** [all] Error 1",
                 "a.c:1:2: error: boom", "ok"]
        hits = extract.find(lines, extract.compile_rules())
        self.assertEqual([(i, r) for i, r, _ in hits], [(1, "compile-error"), (2, "make")])
        self.assertEqual(extract.blocks([1, 3, 20], 30, 1), [(0, 4), (19, 21)])
        self.assertEqual(extract.test_output("not ok 1\n## Test output (expected 'No Errors'):\n## boom\n## \n"),
                         ["boom"])

    def test_render(self):
        self.assertEqual(render.duration(3725000), "1h02m")
        self.assertEqual(render.duration(75000), "1m15s")
        o = render.Out(5, 10000)
        for i in range(10):
            o.add("l%d" % i)
        o.next("jci x")
        t = o.text().splitlines()
        self.assertEqual(len(t), 5)
        self.assertTrue(t[-2].startswith("… 7 more lines"))
        self.assertEqual(t[-1], "next: jci x")

    def test_config_env(self):
        c = config.Config({})
        self.assertEqual(c.jenkins_url, "https://jenkins-pmrs.cels.anl.gov")
        self.assertIsNone(c.jenkins_auth)
        c = config.Config({"JENKINS_HOST": "ci.example.org", "JENKINS_USER": "u"})
        self.assertEqual((c.jenkins_url, c.jenkins_hosts), ("https://ci.example.org", ["ci.example.org"]))
        self.assertIsNone(c.jenkins_auth)           # a user without a token stays anonymous
        c = config.Config({"JENKINS_HOST": "http://ci.example.org:8080/", "JENKINS_USER": "u",
                           "JENKINS_TOKEN": "t", "XDG_CACHE_HOME": "/c"})
        self.assertEqual((c.jenkins_url, c.jenkins_hosts), ("http://ci.example.org:8080", ["ci.example.org:8080"]))
        self.assertEqual((c.jenkins_auth, c.cache_dir), (("u", "t"), "/c/jci"))
        with self.assertRaises(UsageError):
            model.parse_ref("https://jenkins-pmrs.cels.anl.gov/job/a/1/", http.JenkinsClient(c))


@unittest.skipUnless(os.environ.get("JCI_LIVE"), "set JCI_LIVE=1 to run against the real servers")
class Live(unittest.TestCase):
    def test_last_build(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["build", "mpich-review-ch4-ofi", "--no-cache"])
        self.assertIn(code, (0, 1), err.getvalue())
        self.assertIn("mpich-review-ch4-ofi#", out.getvalue())


if __name__ == "__main__":
    unittest.main()

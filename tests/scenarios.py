"""Commands replayed offline against tests/fixtures.json; each has a golden
output file tests/golden/<name>.txt."""

SCENARIOS = [
    # PR with failing checkers, a failing warnings matrix and a pruned build
    ("pr-8037", ["pr", "8037"]),
    ("matrix-warnings-failed", ["matrix", "mpich-warnings#4335", "--failed"]),
    ("failures-warnings", ["failures", "pr:8037/warnings", "--lines", "5"]),
    ("failures-freestyle", ["failures", "mpich-spellchecker#10346"]),
    ("failures-no-junit", ["failures", "mpich-review-unit-testing#9"]),
    # one test failure (timeout) in one cell of a test-suite matrix
    ("build-ch4-ofi-test", ["build", "mpich-review-ch4-ofi#5654"]),
    ("matrix-ch4-ofi-test", ["matrix", "mpich-review-ch4-ofi#5654"]),
    ("failures-ch4-ofi-test", ["failures", "mpich-review-ch4-ofi#5654"]),
    ("log-grep-auto-cell", ["log", "mpich-review-ch4-ofi#5654", "--grep", "session_inter", "-C", "2"]),
    ("artifacts-cell", ["artifacts", "mpich-review-ch4-ofi#5654", "--cell", "asan"]),
    # compile failure in every cell
    ("failures-compile", ["failures", "mpich-review-ch4-ofi#5646"]),
    ("log-errors-cell", ["log", "mpich-review-ch4-ofi#5646", "--cell", "default", "--errors", "-C", "1"]),
    ("log-url-ref", ["log", "https://jenkins-pmrs.cels.anl.gov/view/mpich-review/job/mpich-review-ch4-ofi/"
                            "jenkins_configure=asan,label=ubuntu22.04/5654/console", "--tail", "5"]),
    ("json-failures", ["failures", "mpich-review-ch4-ofi#5654", "--json"]),
    # errors
    ("err-ambiguous-cell", ["log", "mpich-review-ch4-ofi#5646"]),
    ("err-ambiguous-pr", ["build", "pr:8037"]),
    ("err-foreign-host", ["build", "https://example.com/job/x/1/"]),
]

"""Record tests/fixtures.json from the live MPICH Jenkins and GitHub.

Runs each scenario in tests/scenarios.py against the real servers through
recording clients, trims the large responses (console logs, test reports)
so the fixture stays small, and writes every response keyed by request.

    python3 tests/record_fixtures.py

Then regenerate the golden output with JCI_UPDATE_GOLDEN=1 and review the
diff. Builds are pruned after a few weeks, so the scenarios name builds that
existed when the fixtures were recorded.
"""

import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jci import cli, extract, http  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
RECORDED = {}


class RecJenkins(http.JenkinsClient):
    def get_bytes(self, path, query="", cacheable=False):
        data = super().get_bytes(path, query, False)
        RECORDED["jenkins " + path + ("?" + query if query else "")] = data.decode("utf-8", "replace")
        return data


class RecGitHub(http.GitHubClient):
    def get_json(self, path):
        d = super().get_json(path)
        if "/pulls/" in path:
            d = {"head": {"sha": d["head"]["sha"]}}
        if "/status" in path:
            d = {"statuses": [{k: s[k] for k in ("context", "state", "target_url", "description")}
                              for s in d["statuses"]]}
        RECORDED["github " + path] = json.dumps(d)
        return d


def trim(key, text):
    if key.endswith("consoleText"):
        return trim_log(text)
    if "/testReport/api/json" in key and "suites[" in key:
        d = json.loads(text)
        for s in d.get("suites", []):
            failed = [c for c in s["cases"] if c.get("status") in ("FAILED", "REGRESSION")]
            others = [c for c in s["cases"] if c not in failed][:3]
            s["cases"] = others + failed
        return json.dumps(d)
    return text


def trim_log(text):
    """Keep the head, the tail and the neighbourhood of every error-rule hit."""
    lines = text.splitlines()
    hits = extract.find(lines, extract.compile_rules())
    keep = set(range(min(15, len(lines)))) | set(range(max(0, len(lines) - 15), len(lines)))
    for i, _, _ in hits:
        keep |= set(range(max(0, i - 4), min(len(lines), i + 5)))
    return "\n".join(lines[i] for i in sorted(keep)) + "\n"


def main():
    cli.JenkinsClient = RecJenkins
    cli.GitHubClient = RecGitHub
    for name, argv in SCENARIOS:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.main(argv + ["--no-cache"])
        print("recorded", name)
    out = {k: trim(k, v) for k, v in sorted(RECORDED.items())}
    # artifact downloads are not needed offline beyond their presence
    out = {k: (v[:2000] if "/artifact/" in k else v) for k, v in out.items()}
    with open(os.path.join(HERE, "fixtures.json"), "w") as f:
        json.dump(out, f, indent=0, sort_keys=True)
    print("%d responses, %d bytes" % (len(out), os.path.getsize(os.path.join(HERE, "fixtures.json"))))


if __name__ == "__main__":
    main()

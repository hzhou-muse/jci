"""The only two network clients: Jenkins and GitHub, each pinned to one host.

Both issue GET requests only. Credentials go into headers, never into
URLs, error messages or output.
"""

import base64
import json
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .errors import NotFoundError, UnreachableError, UsageError

USER_AGENT = "jci/" + __version__
TIMEOUT = 60


class JenkinsClient:
    def __init__(self, cfg, cache=None):
        self.base = cfg.jenkins_url
        self.hosts = cfg.jenkins_hosts
        self.auth = cfg.jenkins_auth
        self.cache = cache

    def path_of(self, url):
        """Return the path of a Jenkins URL, refusing any other host."""
        if "://" not in url:
            return url
        p = urllib.parse.urlsplit(url)
        if p.netloc not in self.hosts:
            raise UsageError("refusing %s: not the configured Jenkins host (%s)"
                             % (p.netloc, self.hosts[0]))
        return p.path

    def get_json(self, path, tree=None, cacheable=False):
        q = "tree=" + tree if tree else ""
        data = self.get_bytes(path.rstrip("/") + "/api/json", q, cacheable)
        return json.loads(data.decode("utf-8"))

    def get_text(self, path, cacheable=False):
        data = self.get_bytes(path, "", cacheable)
        return data.decode("utf-8", errors="replace")

    def get_bytes(self, path, query="", cacheable=False):
        key = path + ("?" + query if query else "")
        if cacheable and self.cache:
            data = self.cache.get(self.hosts[0], key)
            if data is not None:
                return data
        url = self.base + urllib.parse.quote(path, safe="/=,:@+-._~%") + ("?" + query if query else "")
        headers = {"User-Agent": USER_AGENT}
        if self.auth:
            raw = ("%s:%s" % self.auth).encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        data = _get(url, headers, "Jenkins")
        if cacheable and self.cache:
            self.cache.put(self.hosts[0], key, data)
        return data


class GitHubClient:
    def __init__(self, cfg):
        self.api = cfg.github_api
        self.repo = cfg.github_repo
        self._token = cfg.github_token

    def token(self):
        if self._token is None or self._token:
            return self._token
        self._token = None
        if shutil.which("gh"):
            try:
                out = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                     text=True, timeout=10)
                if out.returncode == 0 and out.stdout.strip():
                    self._token = out.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return self._token

    def get_json(self, path):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
        tok = self.token()
        if tok:
            headers["Authorization"] = "Bearer " + tok
        data = _get(self.api + path, headers, "GitHub")
        return json.loads(data.decode("utf-8"))

    def pr_head(self, pr):
        return self.get_json("/repos/%s/pulls/%d" % (self.repo, pr))["head"]["sha"]

    def statuses(self, sha):
        """Latest status per context for a commit."""
        return self.get_json("/repos/%s/commits/%s/status?per_page=100" % (self.repo, sha))["statuses"]


def _get(url, headers, what):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        shown = url.split("?", 1)[0]
        if e.code == 404:
            raise NotFoundError("%s: not found (%s)" % (what, shown))
        if e.code in (401, 403):
            raise UnreachableError("%s: HTTP %d, authentication failed or rate-limited (%s)"
                                   % (what, e.code, shown))
        raise UnreachableError("%s: HTTP %d (%s)" % (what, e.code, shown))
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise UnreachableError("%s unreachable: %s" % (what, reason))

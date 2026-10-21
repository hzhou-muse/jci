"""Settings come from the environment, never from a file or argv:

    JENKINS_HOST     Jenkins host name or URL (default jenkins-pmrs.cels.anl.gov)
    JENKINS_USER     with JENKINS_TOKEN, authenticate to Jenkins; anonymous otherwise
    JENKINS_TOKEN
    GITHUB_TOKEN     GitHub token; else `gh auth token`; else unauthenticated
    XDG_CACHE_HOME   the cache lives in $XDG_CACHE_HOME/jci (default ~/.cache/jci)

Everything else has a built-in default and a command-line option.
"""

import os

DEFAULT_JENKINS_HOST = "jenkins-pmrs.cels.anl.gov"
GITHUB_API = "https://api.github.com"
DEFAULT_REPO = "pmodels/mpich"
MAX_LINES = 200
MAX_BYTES = 16384
CACHE_MAX_BYTES = 1024 * 1024 * 1024


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        host = env.get("JENKINS_HOST") or DEFAULT_JENKINS_HOST
        self.jenkins_url = (host if "://" in host else "https://" + host).rstrip("/")
        self.jenkins_hosts = [self.jenkins_url.split("://", 1)[1].split("/", 1)[0]]
        user, token = env.get("JENKINS_USER"), env.get("JENKINS_TOKEN")
        self.jenkins_auth = (user, token) if user and token else None
        self.github_api = GITHUB_API
        self.github_repo = DEFAULT_REPO
        self.github_token = env.get("GITHUB_TOKEN", "")
        self.max_lines = MAX_LINES
        self.max_bytes = MAX_BYTES
        cache_home = env.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        self.cache_dir = os.path.join(cache_home, "jci")
        self.cache_max_bytes = CACHE_MAX_BYTES

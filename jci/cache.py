"""Cache for finished builds, which never change.

Entries live under <dir>/<host>/<hash>.gz, keyed by request path and query.
Reads refresh the mtime; writes evict least-recently-used entries past the
size cap. Running builds are never cached (the caller decides).
"""

import gzip
import hashlib
import os
import tempfile


class Cache:
    def __init__(self, root, max_bytes):
        self.root = root
        self.max_bytes = max_bytes

    def _path(self, host, key):
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self.root, host, h[:2], h + ".gz")

    def get(self, host, key):
        p = self._path(host, key)
        try:
            with gzip.open(p, "rb") as f:
                data = f.read()
            os.utime(p)
            return data
        except (OSError, EOFError):
            return None

    def put(self, host, key, data):
        p = self._path(host, key)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as f:
                f.write(data)
            os.replace(tmp, p)
            self._evict()
        except OSError:
            pass  # the cache is an optimization; never fail a command over it

    def _evict(self):
        entries = []
        total = 0
        for dirpath, _, files in os.walk(self.root):
            for name in files:
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        if total <= self.max_bytes:
            return
        for _, size, p in sorted(entries):
            try:
                os.remove(p)
            except OSError:
                continue
            total -= size
            if total <= self.max_bytes:
                break

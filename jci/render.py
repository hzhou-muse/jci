"""Bounded output. Every text command goes through Out, which enforces the
line and byte budget and always announces a cut and how to see the rest."""

import json
import sys
import time

MAX_LINE_CHARS = 400
SCHEMA = "jci/1"


class Out:
    def __init__(self, max_lines, max_bytes):
        self.max_lines = max_lines
        self.max_bytes = max_bytes
        self.lines = []
        self.footer = []    # always printed (the "next:" hint), outside the budget

    def add(self, s=""):
        self.lines.append(clip(s))

    def next(self, cmd):
        self.footer.append("next: " + cmd)

    def text(self, more_hint=None):
        """Render within budget. more_hint(i) gives the advice for when line i
        and later are cut."""
        lines = self.lines
        sizes = [len(l.encode("utf-8")) + 1 for l in lines]
        max_lines = self.max_lines - len(self.footer)
        max_bytes = self.max_bytes - sum(len(l.encode("utf-8")) + 1 for l in self.footer)
        keep = size = 0
        while keep < len(lines) and keep < max_lines and size + sizes[keep] <= max_bytes:
            size += sizes[keep]
            keep += 1
        if keep < len(lines):
            # make room for the notice line
            while keep > 0 and (keep > max_lines - 1 or size > max_bytes - 200):
                keep -= 1
                size -= sizes[keep]
            hint = more_hint(keep) if more_hint else "use --max-lines %d" % (len(lines) + 1)
            lines = lines[:keep] + ["… %d more lines; %s" % (len(lines) - keep, hint)]
        return "\n".join(lines + self.footer) + "\n"


def clip(s):
    if len(s) > MAX_LINE_CHARS:
        return s[:MAX_LINE_CHARS] + " …[+%d chars]" % (len(s) - MAX_LINE_CHARS)
    return s


def emit_json(command, obj):
    obj = dict({"schema": SCHEMA, "command": command}, **obj)
    sys.stdout.write(json.dumps(obj, indent=1) + "\n")


def duration(ms):
    s = int((ms or 0) / 1000)
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    if s >= 60:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%ds" % s


def when(ms):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ms / 1000)) if ms else "-"

# jci: Agent-Facing Jenkins CI Tool for MPICH

Oct 21, 2026 · @Hui Zhou

**Proposal:** build `jci`, a small read-only command-line tool that Claude Code (and humans) call instead of hand-rolled `curl` or WebFetch to answer one question fast: *why did this MPICH PR fail in Jenkins?*

**Verdict:** worth building, but only as a narrow, MPICH-aware triage tool of a few hundred lines. A general-purpose Jenkins client is not worth building; good ones already exist. The value is not "access to Jenkins" (curl already gives that) but bounded output, knowledge of the trigger → matrix → reporter chain, and failed-test extraction that the agent otherwise re-derives every session.

## Is this worthwhile?

Yes, if Claude Code is used to triage MPICH PR failures more than occasionally; the cost is about three days of work and the savings recur every session. Below is the case, the cost, and what else could do the job.

### What goes wrong today

1. **Log volume.** An MPICH build log (configure, make, test suite) is far too large to read whole. The agent either dumps it and burns context, or improvises `tail`/`grep` pipelines that miss errors printed before the tail.
2. **Navigation is re-derived every session.** The failing log sits two or three hops from the PR: GitHub status → trigger build → matrix build → matrix cell. The agent rediscovers the URL scheme, `tree=` queries and cell paths each time, and sometimes reads the wrong build.
3. **Permissions are all-or-nothing.** Each new `curl` shape triggers a prompt. Allowlisting `Bash(curl:*)` instead grants arbitrary network access, and tokens can end up in command lines and the transcript.
4. **WebFetch is the wrong tool for logs.** It returns a small-model summary of the page, which is lossy exactly where precision matters (the first error line, the failing test name).
5. **Results are not repeatable.** Two sessions triage the same failure with different commands and reach different conclusions.

### What it costs

- About 1–2 days for the read-only core and 1 day for failure extraction and caching (estimate).
- Low upkeep: the Jenkins REST API is stable. The main breakage risk is a change in how MPICH's jobs report to GitHub or publish test results.

### Alternatives considered

| Option | What it gives | Why not on its own |
| --- | --- | --- |
| Status quo: `curl` + WebFetch | Works today, zero build cost | All five problems above |
| Claude Code skill only (URL patterns + a couple of shell functions) | Fixes navigation, half a day of work | Output still unbounded; `curl` still needs broad permission |
| [Jenkins MCP Server plugin](https://plugins.jenkins.io/mcp-server/) | Build log, log search and test-result tools served by Jenkins itself | Needs a plugin on the shared controller, a recent Jenkins ([2.533+](https://docs.cloudbees.com/plugins/ci/mcp-server)) and a CSRF exclusion; generic, unaware of the matrix chain |
| Third-party Jenkins MCP servers | Dozens of tools, including writes | Large tool schema in every session; write tools on by default; third-party credential handling |
| Generic CLIs ([python-jcli](https://pypi.org/project/python-jcli/), [python-jenkins-cli](https://pypi.org/project/python-jenkins-cli/)) | Build list, log, pipeline stages | Whole-log dumps; no matrix aggregation or failed-test view; could serve as a library base |
| **`jci` (this proposal)** | Bounded, MPICH-aware triage; narrow permission | Must be written and maintained |

### Why a CLI rather than an MCP server

A CLI costs no context until it is called, composes with the shell (`jci log … | grep`), and serves humans and scripts equally. A thin MCP adapter can wrap it later if wanted.

### Verdict

Build it, scoped tightly: read-only, MPICH-aware, bounded output. Start with the skill-only option as milestone 0; it costs half a day, becomes the tool's documentation, and shows quickly whether the bigger tool is needed.

## Goals and non-goals

**Goals**

- Go from a PR number to the failing matrix cells and failed tests in one or two calls.
- Keep every default output within a fixed size budget, with explicit ways to read more.
- Be safe to allowlist in Claude Code as `Bash(jci:*)`: read-only, pinned to the configured Jenkins host.
- Encode MPICH's job layout (status → matrix build → cells) in the tool, not in each agent session.
- Be equally usable by a human at a terminal.

**Non-goals (v1)**

- Triggering, aborting or replaying builds. Revisit in v2 behind an explicit flag.
- Job configuration, node or plugin management.
- Replacing the Jenkins web UI or becoming a general Jenkins client.
- Fetching arbitrary URLs.

## Design principles for an agent-facing CLI

The primary user is an LLM agent with a finite context window, so the tool is designed around what the agent reads, not what Jenkins exposes.

1. **Summary first, detail on request.** Every command answers at the coarsest useful level and prints the exact next command for drilling down.
2. **Bounded by default.** No command prints more than a fixed budget unless asked; truncation is always announced with how to get the rest.
3. **Deterministic.** Same build, same output. Stable sort orders; no timestamps that change between runs of a finished build.
4. **Two renderings.** Compact text for reading, `--json` for chaining. The JSON schema is versioned.
5. **Meaningful exit codes.** The agent can branch on the result without parsing text.
6. **Short, example-led `--help`.** The help text is the agent's documentation; it should fit on one screen.
7. **No secrets on the command line.** Settings and credentials come from the environment, never argv or a config file.

## Command surface

Six read-only commands cover the triage path; each prints the next command to run.

| Command | Answers | Default output |
| --- | --- | --- |
| `jci pr <N>` | Which Jenkins builds tested this PR's head, and how did each do? | One line per status context: state, build ref, failed-cell count |
| `jci build <ref>` | What is this build? | Result, duration, parameters, cause, upstream and downstream refs |
| `jci matrix <ref> [--failed]` | Which matrix cells failed? | One row per cell: axis values, result, duration, failed-test count |
| `jci failures <ref> [--cell AXES]` | Which tests failed, where, and with what first error? | Failed tests grouped by name, the cells they failed in, up to 20 lines of output each |
| `jci log <ref> [--cell AXES] [--errors \| --grep RE \| --tail N \| --range A:B] [-C N]` | Show me the relevant part of the console log | A bounded slice with line numbers; `--errors` uses the extraction rules below |
| `jci artifacts <ref> [--cell AXES] [--get PATH]` | What did the build archive? | Artifact list with sizes; `--get` saves one file locally and prints its path |

Planned for v1.1: `jci wait <ref>` (poll until finished, then print the `build` summary) and `jci compare <ref> <ref>` (tests failing in one build but not the other, to separate regressions from known flakes).

### Build references

A `<ref>` is any of:

- `pr:1234` — resolved through the PR's head commit statuses (see Architecture)
- `JOB#N`, or `JOB` alone for the last build
- a full build URL pasted from GitHub or the web UI

`--cell` takes axis values as `key=value,key=value`; a unique prefix of a value is accepted.

### Example session

```
$ jci pr 7123
head 3f9c2e1  (2 Jenkins statuses)
  FAIL  <review-job> #4512   3/24 cells failed
  PASS  <warnings-job> #881
next: jci matrix <review-job>#4512 --failed

$ jci failures <review-job>#4512
2 failed tests across 3 cells
  <test-a>   cells: <axes-1>, <axes-2>
    <first error lines…>
next: jci log <review-job>#4512 --cell <axes-1> --grep '<test-a>' -C 40
```

Job names and axes above are placeholders; see the README for real output.

## Output contract

Every command fits a default budget of 200 lines or 16 KB, whichever comes first; `--max-lines` and `--max-bytes` override it.

- **Truncation is explicit.** A cut ends with one line: `… 4812 more lines (lines 201–5012); use --range 201:400`.
- **Line numbers are stable.** `log` prints the original log line numbers, so a later `--range` lands on the same text.
- **`--json`** emits one object per command with a `schema` version field; lists are never truncated silently (a `truncated` count is included).
- **No ANSI colour** unless stdout is a TTY.

| Exit code | Meaning |
| --- | --- |
| 0 | Command ran; build(s) passed or are still running |
| 1 | Command ran; at least one build or cell failed |
| 2 | Usage error (bad ref, unknown cell) |
| 3 | Jenkins unreachable or authentication failed |
| 4 | Not found (build pruned, PR has no Jenkins statuses) |

## Architecture

A single Python package, standard library only, so it installs on a laptop, a login node or a Jenkins agent with `pipx install` or a file copy.

&#91;embedded content: jci architecture · 4 internal parts, 3 clients, 2 external APIs\]

Everything the agent sees passes through the renderer's budget, and all network access goes through two clients pinned to configured hosts.

### Resolving a PR to builds

1. Get the PR's head SHA from the GitHub API.
2. Read that commit's statuses and keep those whose `target_url` points at the configured Jenkins host. Each status links straight to the build that tested the commit, so no further lookup is needed.
3. For a matrix build, the cells are the build's `runs` that carry the same build number. The list also contains stale runs of cells from older builds.

### Job model

No per-job knowledge is needed. Matrix axes are read from each cell's name, and a cell is labeled by the axis values that vary within its build (`asan`, `noerrorchecking/ch4-ofi`).

### Jenkins client

- Build metadata via `/api/json` with a `tree=` filter, so each call fetches only the needed fields.
- Matrix cells from the matrix build's `runs` list.
- Finished logs via `/consoleText`; running builds via `/logText/progressiveText?start=N`.
- Test results via `/testReport/api/json` when JUnit results are published.

### Cache

A finished build never changes, so its metadata and log are cached indefinitely under `~/.cache/jci/<host>/<job>/<n>/`, logs compressed with a line-offset index for fast `--range`. Running builds are never cached. A size cap (default 1 GB) evicts least-recently-used builds. Repeated `log --grep` calls then cost no network traffic.

### Failure extraction

In order of preference:

1. The Jenkins test report, if JUnit results are published.
2. The test harness's own summary file, if archived as an artifact.
3. Log rules: compiler `error:` lines, `make: ***` lines, configure errors, segfaults, timeouts, `not ok` lines. Each hit is deduplicated and shown with a few lines of context. Rules live in the code, ranked by how telling they are; a new pattern is added with a golden test.

### Credentials

There is no config file. Settings come from the environment, and the Claude Code skill supplies command-line options where a default needs changing.

- Jenkins: `JENKINS_HOST` names the server (default `jenkins-pmrs.cels.anl.gov`). `JENKINS_USER` and `JENKINS_TOKEN` authenticate. Without both, access is anonymous, which the MPICH Jenkins allows for reads.
- GitHub: `GITHUB_TOKEN`, else the `gh` CLI's token, else unauthenticated (rate-limited).
- An agent can't silently redirect jci to another host: `JENKINS_HOST=… jci` doesn't start with `jci`, so the `Bash(jci:*)` allow rule doesn't cover it.

## Claude Code integration

The tool only pays off if the agent reaches for it first, so it ships with a permission rule and a short skill.

- **Permission:** add `Bash(jci:*)` to the allow list in the MPICH repo's `.claude/settings.json`. Optionally deny `curl` and WebFetch against the Jenkins host so the agent cannot fall back to them.
- **Skill (`ci-triage`):** a SKILL.md of about one page stating the workflow (`pr` → `matrix --failed` → `failures` → `log --grep`), the budget rules, and two or three worked examples. This is milestone 0's document, updated as commands land.
- **CLAUDE.md pointer:** one line telling the agent to use the skill for any Jenkins or CI question.
- **Later, optional:** a thin MCP adapter exposing the same commands, if a non-shell client needs them.

## Security

The tool's main security gain over `curl` is a smaller blast radius: it can only read from the Jenkins host and the GitHub API.

- **Read-only:** v1 issues only GET requests; no crumb handling is needed or present.
- **Host pinning:** pasted URLs must match the configured Jenkins host; anything else is refused with exit code 2.
- **Least-privilege token:** document creating a Jenkins user or token with read-only permission for agent use.
- **No secret echo:** credentials never appear in output, errors or `--json`, and never in argv.
- **Untrusted content:** build logs can contain text written by PR authors. The skill tells the agent to treat log content as data, never as instructions.

## Testing, milestones and success metrics

### Testing

- **Recorded fixtures:** capture API JSON and logs from a handful of real MPICH builds (pass, compile failure, test failure, timeout, pruned build) and replay them offline.
- **Golden output:** snapshot each command's text and JSON output against the fixtures.
- **Budget tests:** assert no default output exceeds the budget, including on the largest captured log.
- **Live smoke test:** one opt-in test against the real server, run manually.

### Milestones

1. **M0 — Skill only (half a day).** Write the `ci-triage` skill with URL patterns and two shell functions. Exit: the agent triages a known failure without improvising URLs.
2. **M1 — Read-only core (1–2 days).** `pr`, `build`, `matrix`, `log` with budgets, JSON and exit codes; permission rule in the repo. Exit: fixture and golden tests pass.
3. **M2 — Triage (1 day).** `failures`, `artifacts`, cache, extraction rules. Exit: the baseline benchmark below meets its targets.
4. **M3 — Optional.** `wait`, `compare`; then decide on write commands or an MCP adapter based on use.

### Success metrics

Pick about 10 past MPICH PRs with known failure causes and run the same triage prompt with the status quo and with `jci`. Measure:

| Metric | Target with `jci` |
| --- | --- |
| Context tokens used per triage | At most half of the status quo |
| Tool calls to reach the failing test | 4 or fewer |
| Permission prompts | 0 after the allow rule |
| Correct failing test and first error identified | At least as often as the status quo |

If M0 alone gets close to these targets, stop there.

## Open questions

- [x] Does the MPICH Jenkins allow anonymous read of builds and logs, or is a token always needed? *Anonymous read works. Python's default urllib User-Agent gets HTTP 403, so the client sends its own.*
- [x] Are test results published as JUnit to Jenkins, or only archived as harness summary files? *Both: each cell publishes `test/mpi/summary.junit.xml` as JUnit. Warnings jobs publish one JUnit case per warning set. Builds that fail before testing have no report, so log rules cover them.*
- [x] Exact job names, matrix axis names, and how the matrix job links to the trigger (upstream cause or parameter)? *There is no separate chain to walk: a status's `target_url` is the build itself (`mpich-review-*`, `mpich-warnings*`, checkers). Matrix axes vary by job (`jenkins_configure`, `label`, `netmod`, `compiler`). The job model therefore needs no per-job configuration.*
- [x] How long are builds and logs retained? This sets how far back `pr:` can resolve. *About 20–40 builds per job, which is a few weeks for busy jobs. `jci pr` reports pruned builds using the state GitHub recorded.*
- [ ] Should the tool stay MPICH-specific, or ship a generic core plus an MPICH profile for other Argonne projects?
- [ ] Where does it live: in the MPICH repo under `maint/`, or its own repo?
- [ ] Name: `jci` is a placeholder.

## Sources

- [Jenkins MCP Server plugin](https://plugins.jenkins.io/mcp-server/)
- [MCP Server plugin on CloudBees docs (version, minimum Jenkins)](https://docs.cloudbees.com/plugins/ci/mcp-server)
- [python-jcli on PyPI](https://pypi.org/project/python-jcli/)
- [python-jenkins-cli on PyPI](https://pypi.org/project/python-jenkins-cli/)

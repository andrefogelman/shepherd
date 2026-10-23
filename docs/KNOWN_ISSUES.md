# Known issues

Open defects, with what is actually established about each. An entry stays open
until a test pins the fix, then moves to the bottom with the mechanism kept —
the mechanism is the part worth remembering.

## Open

### The local gate runs the worker's own tests outside the jail

**Established:** the native gate refuses a proposal without tests, then runs
the tests the worker wrote, and `pre_gate_cmd`, on the host with no jail
(`supervisor._run_gate`, `run_pre_gate`), with the user's environment
minus `GATE_ENV_STRIP`. A worker that wants out of the jail does not need a socket: a
`conftest.py` or a test it wrote runs at gate time with the user's rights.
Hiding the command servers (below, under Fixed) does not contain such a
worker. **Next:** run the local gate and `pre_gate_cmd` through the same
Landlock and bubblewrap jail, writable only in the staged tree; until then
the trust boundary in the manual says the gate executes worker-written code.

### Sockets the jail does not hide

**Established:** Landlock below ABI 6 lets a confined command connect to any
Unix socket by path and to any abstract socket, and bubblewrap here does not
unshare the network namespace, which the worker needs for the API. On macOS the
Seatbelt profile is `(allow default)` plus write denials, so connects are
allowed unless a rule names the socket. shepherd-ai 0.3.12 hides or denies the
sockets of the servers that run commands for the user where it can name them
(below, under Fixed). It does not block, and warns about, a Docker daemon the
user can reach (a socket, a docker context, or plain TCP on loopback), an X
display, a session bus at an abstract address, and an sshd on this host the
user logs in to, on Linux and on macOS. shepherd-dev unsets `TMUX`, `STY`,
`DBUS_SESSION_BUS_ADDRESS`, `DOCKER_HOST`, `DISPLAY`, `XAUTHORITY`,
`SSH_AUTH_SOCK` and `SSH_AGENT_PID` for the worker, with or without
`worker.harden`. Still reachable: an ssh agent whose socket `SSH_AUTH_SOCK`
does not name (a second agent, or one the user started elsewhere), which the
start-of-run warning names when it sits where agents put their socket
and something listens there (shepherd-ai 0.3.17) and does not find when
started with `-a` elsewhere;
a dbus session bus at an abstract address; a server started during the run
with its socket somewhere no mask covers (`tmux -S /elsewhere`, a screen
under `/run/screen/S-<user>`, whose root-owned parent shepherd-ai cannot
pre-create the directory in); a socket created after the process's first
launch in a subdirectory of a path bound back into a hidden directory (the
walk is redone only when that path's own mtime changes, and the check inside
the jail walks it once per process); a socket directory another user owns, or
a symlink another user owns or that points at something not this user's (left
unmasked rather than followed, and reported); an editor's or terminal's
remote-control socket;
any other same-user server that executes commands. **Next:** Landlock ABI 6
(scoped abstract sockets) or a filter on `connect(AF_UNIX)`.

## Fixed

### The pack handed the worker an open code block (cuts and file blocks)

**Was:** `workspace_instructions` keeps what fits of `INSTRUCTIONS_BUDGET`
once the marker's reserve is taken (3,980 characters of the repo's
instruction file) and appends the marker. The cut is a raw character offset.
With the Gate section right after the worker-facing sections, it fell inside
the Gate's bash fence, so the prefix the worker read ended with an open code
block that swallowed the marker and whatever the pack appended next. Moving a
prose section in front of the Gate fixed that, and the cut then fell inside
`` `claude` `` in that section's first bullet: an open inline span, same
effect. Both were found by the review bot on the PR, not by the suite,
because the budget test only checked that the right sections preceded the
marker. The same held for every other text the pack copies in: a planner
sketch over `PLAN_TEXT_CAP` was cut at a raw offset too, and a file block
(whole under `FULL_FILE_LIMIT`, or a Markdown skeleton's first lines) that
ended inside a fence, such as an unfinished Markdown file or an unpaired
fence in a docstring or comment, swallowed every `== FILE:` section after it.

**Fix:** the section after "Language and style" is plain prose near its
start, and `tests/test_agents_md_budget.py` asserts that the visible slice
has an even number of fences and, with fences removed, an even number of
backticks. Each assertion fails against the AGENTS.md of the commit it was
added for (4faad35: one fence before the marker; 687e4f6: the cut after ``
(`claud ``). That protected this repository's file only; the pack itself
still cut at a raw offset, so any other repository's AGENTS.md (or a planner
sketch over `PLAN_TEXT_CAP`) could hand the worker an open block.
`cut_to_fit` now cuts on the last empty line (two consecutive newlines)
before the budget that leaves every code block closed, and falls back to the
raw offset only when no empty line in the second half of the budget does (one
long paragraph, or a block that opened before the halfway point); the
fallback closes an open block with its own fence (indentation and run, so a
block opened inside a list item is closed inside it) and drops an open inline
span back to before its opening backtick, unless the real paragraph never
closes that run within the budget past the cut: the callers put a blank line
after the marker, so such a run is literal in CommonMark and stays. A span is
judged per paragraph (a blank line, a fence line, a heading, a thematic
break, a setext underline, a non-empty list item or the start of a block
quote ends one, inside a quote as well), before the marker. Text that fits
but itself leaves a block open gets that block's closer, so what the pack
appends is not swallowed either. List items are tracked as containers: a
block opened inside one (on the marker line or later) closes within the
item's column window, and a dedented line ends the item and the block with no
closer (a line dedented out of a block is never lazy: the opener ended the
paragraph); with no block open, a dedented line that is a lazy continuation
of the item's paragraph (prose after prose, even behind a quote marker,
starting no block and bearing no marker) keeps the item instead, and a marker
on a dedented line starts an item, as in cmark, where the list and not the
paragraph is that line's container; a thematic break (`* * *`) is never read
as markers. A blank line is spaces and tabs only, as in CommonMark (an NBSP
line is paragraph text, and a vertical tab or form feed is content). A line
four or more columns past its container with no paragraph to continue is
indented code, not a paragraph, so the marker after it is an item (and so is
a wide-gap item's content, even under a paragraph, so the line after it is
not lazy); an item that began blank ends at the next blank line; and a `===`
right behind a marker opens that item's paragraph instead of underlining an
outer one. Not tracked: an item indented four or more columns in its
container (indented code; tabs are read at their 4-column stops), a block
quote as a container (a fence opened behind `>` is read as ending with the
quote, which is where CommonMark ends it too, and its lines are code, so no
line after them continues a paragraph), and HTML blocks (a line starting, up
to three spaces in, with an open or closing tag, `<!--`, `<?`, `<!X` or
`<![CDATA[`): the block is not tracked, so a fence run inside it is read as a
fence; the closer the cut then appends is, to CommonMark, a fence opener
after the HTML block ends (at a blank line, or at the end marker for `<pre`,
`<script`, `<style`, `<textarea`, `<!--`, `<?`, `<!X` and `<![CDATA[`: the
closing tag, `-->`, `?>`, `>` or `]]>`), and the marker and the section the
pack appends after it land inside that block. An HTML comment or `<pre` left
open before the cut swallows the marker the same way, as raw HTML. Markdown
that opens a fence directly under an HTML line with no blank line is already
read this way by cmark and GitHub, so the pack does not try to repair it. The
span check is conservative rather than CommonMark-exact: a backtick run left
pending in a paragraph hides the spans after it, a heading line is not
rescanned, and a prefix's last line that continues the paragraph is judged as
cut, so a cut can land inside a span CommonMark would have closed; nothing
open reaches the worker either way, because the callers put a blank line
after the marker, and a blank line after each section header keeps the file's
first line from continuing the header's paragraph. A file met with a
remaining budget of `_MARKER_RESERVE` or less is left out of
`workspace_instructions` instead of being reduced to the marker alone, as it
was. The file blocks of the pack got the same treatment: a file shown whole
(up to `FULL_FILE_LIMIT`) or as a skeleton (its first lines, for a type with
no signature prefixes, such as Markdown) that leaves a fence open gets its
closer (`_closed`), since an unfinished Markdown file, an unpaired fence in a
docstring or one in a comment swallowed every `== FILE:` section after it;
the repository tree is closed the same way (a directory named ``` would
otherwise open a fence there); a path is printed on one line, with control
characters escaped (a newline in a file name put the rest of a header on a
line of its own); a lone CR in a file is made a line ending before the check;
and every section header is followed by a blank line, so the first line after
it starts a block of its own. An HTML block a file leaves open (`<!--`,
`<pre>`, `<script>` with no end) still swallows what follows: not tracked,
like HTML blocks in the cut. Pinned by `tests/test_agents_md_budget.py`,
`tests/test_contextpack.py::CutToFit`,
`tests/test_contextpack.py::WorkspaceInstructionsBudget` and the
`test_plan_text_*` cases,
`test_a_planned_files_skeleton_does_not_leave_a_block_open`,
`test_a_whole_file_that_ends_inside_a_block_does_not_swallow_the_next`,
`test_whole_files_from_keywords_and_test_contracts_are_closed_too`,
`test_a_path_that_looks_like_a_fence_does_not_swallow_the_pack`,
`test_a_file_name_with_a_newline_stays_on_its_header_line`,
`test_every_path_the_pack_prints_stays_on_one_line` and
`test_a_lone_cr_in_a_file_is_a_line_ending_to_the_closer` in
`tests/test_contextpack.py::ContextPackEnrichment`.

### Bytecode the worker's own test run left behind became part of the proposal

**Was:** with the gate command in its prompt, the worker runs the suite before
finishing — which is the point — and Python leaves `__pycache__/*.pyc` in the
working copy. The substrate captures every changed path, and
`read_changeset_entries` read them all: on the first real run after the prompt
change, a two-file proposal came back as four "changed files", two of them
bytecode. Their binary content then reached the reviewer's diff, where
`decode(errors="replace")` keeps a NUL as a real character, and the reviewer's
launch died before it started: `ProviderInvocationError: embedded null byte`
— a str with a NUL cannot be an argv element. `IGNORED_DIRS` had
`__pycache__` in it all along; nothing on the changeset path consulted it.

**Fix:** `read_changeset_entries` skips build residue before reading it —
any path with a segment in `CHANGESET_NOISE_DIRS` (`IGNORED_DIRS` plus the
usual tool caches) or a bytecode suffix. `_one_file_text` names a binary file
and its size instead of rendering it, and `render_prompt` spells out any NUL
that still reaches it, so no value can take a launch down. Pinned by
`tests/test_changeset_noise.py` and `tests/test_promptrender.py`.

### A stale credentials file shadowed a live keychain login

**Was:** established on the first real run after the auth preflight shipped.
The substrate's credential lookup reads `~/.claude/.credentials.json` before
the macOS keychain. On this machine that file dated from a month earlier and
its token had expired 690 hours before; the keychain held the login the CLI
actually uses, good for another six hours. So the CLI worked, the preflight's
probe succeeded, and the substrate still seeded the dead token and refused
the launch. This is the shape behind the history's `Not logged in` failures:
nothing else on the machine complains about a file the CLI no longer reads.

**Fix:** `auth_preflight` reads the expiry of each source the substrate might
seed from (the file, and on macOS the keychain) and, when the token is still
expired after the probe, names this case outright — which file, how long
expired, which live login it shadows, and the two remedies (refresh or remove
the file; or point `CLAUDE_CONFIG_DIR` at a directory holding a current copy,
which is how the smoke run itself proceeded without touching the user's
file). A token still expired after the probe now stops the run before the
pack rather than warning and letting the substrate refuse it. Not changed:
the substrate's lookup order, which lives in the fork of shepherd-ai and
would be the right place for "prefer the freshest non-expired source".
Pinned by `tests/test_auth_preflight.py`.

### The worker had no toolchain, and went looking for one elsewhere

**Was:** the jail's working copy is materialized from a git tree, so
everything the repository gitignores is absent by construction — `deps/`,
`_build/`, `node_modules/`, `target/`. `jail_env` and `jail_seed` gave the
GATE a warm cache (it runs unjailed, from the parent); the worker could read
that cache and write nowhere, since the jail denies every write outside its
working copy. A worker on a compiled language therefore could not compile,
burned turns discovering that (`which mix elixir`, `ls _build`), and a defect
the compiler names in seconds cost a whole attempt plus a gate: 35
`tests_failed` attempts at a median 888 s. Twenty-odd workers then found a
way: they shipped the repository to a remote host over ssh and compiled it
there — the escape the launch policy now closes, but the need was real.

**Fix:** the jail accepts more than one writable root, and the launch seam
already sees the confinement spec. `seed.LaunchSeed` deals each launch —
every worker attempt, and the reviewer — its own copy of each `jail_seed`
origin (copy-on-write where the filesystem has it), adds that copy to the
launch's writable roots, and names it in the variable for that launch only,
through the env prefix the substrate puts before the CLI. `jail_seed_links`
does the same for caches that must sit IN the tree (`node_modules`): a copy,
a symlink at the path, and the path kept out of the changeset. The copies
are destroyed when the launch returns, so two lanes, a worker and a gate, or
an attempt and its retry never share a directory. The gate keeps the
command-level copy it had. Pinned by `tests/test_launch_seed.py`.

### The planner's targets were the first thing the pack dropped

**Was:** `build_pack` filled the budget with keyword-scored files first and
force-included the planner's targets afterwards, "so the worker always gets
the files the planner named" — with whatever budget was left, which on a
real repo was none: the pack hit its 25k ceiling on 100% of recorded runs,
98 of 120 runs had planned targets, and 1 landed by that path. The tree
listing (300 entries of arbitrary length) and the instructions file took
their share before any file did, and nothing kept room for the files the
pack exists to carry.

**Fix:** the planner's targets go in first; `FILES_RESERVE` (15k of the 25k)
is kept for file content, and the tree listing is trimmed to what the
reserve leaves, saying how many entries it dropped; the plan text is capped.
Pinned by `tests/test_pack_budget.py`.

### The reviewer read the wrong tree

**Was:** the reviewer ran in the workspace's pre-change tree, with the
proposal handed to it as a unified diff and, since 0.1.34, as a side
directory of the changed files. So the tree it could `cd`, `grep` and open —
the natural thing for a code reviewer to do — was the one tree the proposal
was NOT in. Measured on 102 real reviews: a median 29 tool calls, 18 of them
Bash navigating that tree. Once it reported an entire feature "missing" that
the diff in front of it added; the prompt had to grow a paragraph forbidding
that inference.

**Fix:** the proposal is written into the reviewer's working copy before its
CLI starts (`_PreparingExecution` on the launch seam, fed the applied
proposal's path from the invocation's own kwargs), so the working directory
IS the tree under judgment and the prompt says so. Custody got stronger, not
weaker: the proposal's paths come back in the reviewer's changeset, and
instead of being exempted by path they are compared by BYTES — a proposal
file the reviewer edited now invalidates the verdict, which the old
drift-by-path subtraction could not detect. Pinned by
`tests/test_review_overlay.py`.

### A dead login or a spent allowance cost an attempt to discover

**Was:** the substrate seeds the host CLI's subscription login into the
jailed worker's scratch config and refuses a blob whose `expiresAt` has
passed. Nothing more could happen from inside the jail: the CLI there holds a
copy, so a refresh it performs is written nowhere that survives the launch.
In the history, 8 attempts died on `Not logged in` / `OAuth access token has
been revoked` and 16 on `You've hit your weekly limit` — each after the pack
was built, the worktree re-adopted and the workspace forked, and each
labelled a run failure.

**Fix:** `preflight.auth_preflight` runs before the pack. A missing
credential fails in zero seconds. A subscription token that is expired or
expires inside a run's reach (45 minutes: a worker attempt plus its review)
is refreshed by one unjailed `claude -p ok` with the REAL config dir, where
the CLI writes the refreshed token back to the store the next seeding reads
from. The same probe's answer is read for a spent allowance or a dead login,
either of which stops the run with the reason and, for the allowance, when
it resets. A probe that merely could not run (network, timeout) is a warning
and the run proceeds. `preflight.auth_probe: true` in `.shepherd-dev.json`
runs the probe on every run; `SHEPHERD_DEV_NO_AUTH_PREFLIGHT=1` skips the
whole check. Pinned by `tests/test_auth_preflight.py`.

### A worker reaped at its budget was recorded as an unexplained run failure

**Was:** the budget hard-kill has two layers. When the launch perl's alarm
fires first — the normal case — the worker's process group dies, the perl
exits 124, and the substrate raises `ProviderInvocationError("confined body
refused (rc=124): …")` out of `workspace.run`. The watchdog, the second
layer, then finds nothing to kill and never fires, so `wd.fired` is False and
the exception fell through to the generic branch: verdict `run_failed`, error
"the agent run itself failed", and the retry told to "work efficiently"
instead of receiving the timeout guidance. In the history: 13 attempts of
about 915 seconds, all labelled `run_failed`, 3.3 hours whose cause the
record did not name. The same held for the framework's own alarm path, which
raises `BudgetExhausted`.

**Fix:** `_budget_killed` recognises both shapes (the exit code 124 in the
message, the exception's name, the framework's "budget exceeded" text), and
`develop` records the attempt as `timed_out` naming the budget it exceeded,
with `_TIMEOUT_GUIDANCE` for the retry — the same outcome the watchdog path
already produced. Alongside: a repo may set `limits.worker_budget`,
`limits.gate_timeout` and `limits.max_attempts` in `.shepherd-dev.json`
(flags still win), so a repository whose features need a longer wall clock
declares it once instead of on every command line. Pinned by
`tests/test_budget_kill.py`.

### `optimize` hung two minutes per replay case, then scored the case as a failure

**Was:** `optimize._replay` turns a pinned worktree into a workspace with
`shepherd-dev init` in a subprocess that inherited the parent's stdin.
`cmd_init` asks the review-panel size with `input()` unless `--review-panel`
is passed. On a closed stdin `input()` raises EOF and the default is kept —
the case every test exercised. On an OPEN stdin that never closes — a
terminal, or the pipe a harness holds — `input()` blocks until the init's
120-second timeout, `_replay` returns False, and the candidate prompt is
scored on a workspace that never existed. Six replay cases is twelve minutes
of hanging per `optimize`, every one of them counted against the candidate.
Reproduced: the replay test passes in 1.7 s with stdin from `/dev/null` and
fails after exactly 120 s with stdin from a pipe held open. Nothing said
why: an init that exited non-zero returned False in silence.

**Fix:** both replay subprocesses run with `stdin=DEVNULL` — a replay is
never interactive — and `_ask_review_panel` asks only when stdin is a
terminal, taking the default otherwise, so no caller of `init` can block on
it again. An init that fails now says so on stderr with the tail of its
output, the same way a timed-out one already did. Pinned by
`tests/test_optimize_replay.py` (stdin on both calls) and
`tests/test_review_panel.py` (no prompt off a terminal).

### No run recorded what it cost or which model produced it

**Was:** the worker's stream-json ends with a `result` event carrying token
usage, dollar cost, turn count and API time, and opens with a `system/init`
event naming the model, the tools and the MCP servers of the session. The
stream tailer translated tool calls into events and dropped both of those.
The substrate parses them too, but its durable trace is discarded at the next
run's re-adoption. So across 274 recorded runs there was not one token count,
no cost, and no evidence of which model had served a single attempt —
duration was the only proxy, and `--model`/`--effort` were never passed, so
the model was whatever the CLI defaulted to that day.

**Fix:** the tailer records `worker.init` (model, tool count, MCP server
names, CLI version) and `worker.result` (tokens in/out, cache reads and
writes, cost, turns, wall and API time, the models that served the session),
and files the result under the thread that launched it, so `develop` can
read it back — `Attempt.usage` per attempt, `ReviewVerdict.usage` for the
reviewer (the speculative one included). It reaches the console summary,
`--review-report`, the history file, the proposal manifests and `status`,
which sums tokens, cost and launches per run and names the models. A panel
reviewer's launches run on threads `develop` does not own and are recorded
in the event log only.

Pinned by `tests/test_telemetry.py`: extraction from both events, the
tailer's slot, the hook handing the result back once to the launching thread,
`develop` recording per-attempt and review usage, both renderers and the
history showing it, and `status` summing a run.

### Any finding rejected the proposal, so most rejections meant nothing

**Was:** the verdict schema had `approved` and a flat list of `issues`, and the
prompt said an empty list was the only acceptable companion to an approval.
So a reviewer that noticed anything — "Minor/non-blocking: prefer `defp`",
"Nit: trailing whitespace" — put it in `issues` and answered `approved:
false`. Measured on the 100 verdicts in the history with a real reviewer: 58
rejections, of which the human then accepted 34. A rejection acted on four
times in ten is not a verdict. Every one of them also disabled `--auto-settle`
for that run, and with `--review-rounds` each spent a whole worker attempt
reworking a quibble.

**Fix:** severity is part of the finding. `issues` is a list of
`{"severity": "blocking" | "advisory", "text": …}`; the prompt defines both
("would you hold the change back for it?") and binds `approved` to them —
true when nothing is blocking, false when anything is. `parse_review_issues`
splits the list (a bare string, the old shape, is classified by the label it
gives itself: minor/nit/low/non-blocking read as advisory, anything else and no
label at all as blocking — fail closed). `ReviewVerdict.issues` now holds only
blocking findings and a new `advisories` field the rest; the ledger, rework
rounds, `flag_gate_contradiction` and auto-settle see only the blocking ones,
while summaries, reports, history, manifests and repo memory carry both.

One correction, in one direction, under three conditions at once: the
reviewer wrote the JSON literal `false`, listed at least one advisory finding,
and listed no blocking one. That verdict is recorded as approved, with the
correction written into the summary the human reads — the reviewer named
nothing it would hold the change back for. Nothing else moves: a malformed
`approved` ("false", 1, missing) stays a non-approval, a rejection with no
finding at all stays a rejection (it reaches the human as "rejected but left
no actionable finding"), and an approval alongside a blocking finding is never
overturned. The Codex reviewer's prompt and parser follow the same shape.

Pinned by `tests/test_review_severity.py`: classification of objects and
labelled strings, the fail-closed defaults, the one-way correction, the
aggregate, the renderings, and `develop()` spending a rework round on a
blocking finding and none on an advisory one.

### The jailed worker reached the user's ssh agent, MCP connectors and sub-agents

**Was:** the jail confines writes to the run's clone and nothing else. The
worker inherits this process's whole environment, reads any path, and has the
network. Established from the event logs of real runs: workers on a compiled
language with no toolchain in the clone read `~/.ssh/config`, found
`SSH_AUTH_SOCK` in their environment, and ran `ssh <host> "docker run …"` to
compile and test the repository on a remote machine — twenty-odd runs across
three days, one of them ending in `rm -rf` of the directory it had created
there. The reviewer called the account's MCP connectors (a database
`list_projects`, `list_tables`). Both agents spawned sub-agents (`Agent`, 7
launches: model work outside the trace and the budget) and spent turns on
`TaskCreate`/`TaskUpdate` bookkeeping (320 calls across 19 runs, median 18 a
run). A repository's own `.claude/settings.json` was also loaded into the
session, hooks included.

**Fix:** `launch.LaunchPolicy` + `harden_argv`, applied by the transport
`set_worker_budget` installs to the framework's real argv: `--disallowedTools`
for sub-agents, task-list, tool-search, web and plan-mode tools;
`--strict-mcp-config` with no servers; `--no-chrome`; `--setting-sources user`
(the redirected, empty config dir — nothing committed to the repo under review
reaches the session); and `env -u NAME` for every credential-shaped variable
present in the environment (a fixed list plus name patterns such as `_TOKEN`,
`_API_KEY`, `_PASSWORD`), inserted into the env prefix the substrate already
puts in front of the CLI. `ANTHROPIC_*`/`CLAUDE_*`, `jail_env` keys and the
repo's `worker.env_keep` are never unset; `worker.harden: false` turns it all
off. Each launch records a `worker.launch` event naming what was denied and
which variables were unset (names only).

Not fixed here, and documented: network reach itself. The jail's profile
cannot filter by host, and the API needs the network; a worker that finds
credentials elsewhere (a file in the repository, a key on disk) can still use
them. Pinned by `tests/test_launch_policy.py`, including one case against
the framework's real `command_argv`.

### The worker read its prompt as a source dump and an escaped JSON blob

**Was:** what the jailed Claude actually received was not the task's prompt.
The substrate's workspace-control lane builds its own envelope
(`shepherd_dialect.workspace_control.runtime_provider._claude_runtime_prompt`):
a generic preamble, the ENTIRE source of `tasks.py` as a fenced "task contract"
(every prompt of every task, for every run), then the run's arguments as one
`json.dumps(indent=2)` block with `ensure_ascii` on. So the 25k-char context
pack and the 5k-char feature arrived as single-line strings full of `\n`, with
every accented character spelled `\u00e7`, and the actual instructions sat
inside the Python block as a docstring literal. The pack also travelled under
the argument named `guidance`, whose own prompt text says "feedback from a
previous failed attempt" — every first attempt was told its repository context
was a failure report. Nothing pinned the shape, so nothing noticed.

Measured on 182 worker attempts from the event logs: the first tool call was
`pwd && ls -la` (or a variant) in most of them, and the median attempt spent
18 tool calls exploring before its first edit, pack or no pack — the pack was
there, but not in a form a model reads.

**Fix:** `promptrender.render_prompt` renders the prompt as a document — the
task's own prompt first, then one titled section per argument, long values
fenced so nothing inside them can pass for a section of the prompt itself —
and the transport `set_worker_budget` installs sends that instead of the
envelope. The invocation the substrate hands the transport carries the task id
and the raw kwargs, which is everything the render needs; a task this module
does not know keeps the substrate's text untouched. `develop` now passes the
pack as `context`, the failure feedback as `guidance`, and the gate command as
`gate`, and the reviewer receives `gate` too (the command that already judged
the proposal, and its verdict). Each launch records a `worker.prompt` event
(shape only: task, size, sections present).

Pinned by `tests/test_promptrender.py`: the rendered document (order, fencing,
no escapes, no envelope), the transport handing it to the provider and
recording the event, a foreign task keeping the envelope, and `develop`
passing the three inputs apart.

### The jailed worker and reviewer could not run a single command

**Was:** on macOS, every `Bash` call inside a run failed and neither agent said
so as a failure — they degraded. The worker wrote code it never executed; the
reviewer reported "Bash is blocked in this sandbox (EPERM), so this review is
by inspection of the diff against the spec — no execution." A supervised cycle
that never runs anything is most of the supervision gone, and it looked like a
clean pass.

```
EPERM: operation not permitted, mkdir '/private/tmp/claude-<uid>/<flattened-cwd>'
```

Three facts meet:

1. The worker runs under a syscall jail (`launch_confined`) whose writable
   roots are the run's clone. Anything written outside is denied, and a
   seatbelt denial surfaces to the CLI as `EPERM`.
2. The framework redirects `HOME`, `CLAUDE_CONFIG_DIR` and `TMPDIR` into a
   per-run scratch inside that clone.
3. The Claude CLI does not read `TMPDIR` for its own Bash-sandbox scratch. On
   darwin that root is a hardcoded `/tmp`; `CLAUDE_CODE_TMPDIR` is the only
   override. So the sandbox scratch was the one thing still resolving outside
   the writable roots.

Established by probe: a bare CLI in the same clone path, unjailed, runs `Bash`
fine, and the flattened scratch dir is created without complaint — so it is
neither the path shape nor a filesystem permission. Redirecting `TMPDIR` alone
does not move the sandbox scratch.

**Fix:** `_with_sandbox_tmpdir` adds `CLAUDE_CODE_TMPDIR` to the launch's env
prefix, set to whatever the framework chose for `TMPDIR` — read off the argv,
so a framework that moves its scratch moves this with it. Pinned by
`tests/test_worker_sandbox_tmpdir.py`, whose last case fails loudly if the
framework ever stops carrying `TMPDIR` there, since the rewrite would silently
degrade to a no-op and restore the defect.

### The local gate stage killed the run while tearing itself down

**Was:** any supervised cycle in an affected repo died mid-gate. The process
raised inside teardown, so the run neither passed nor failed, it just ended.

`_link_dep_dirs` symlinks the repo's dependency directories (`.venv`,
`node_modules`) into the staged copy so the gate can import them without a
reinstall. Teardown then removed the stage with
`shutil.rmtree(root, ignore_errors=True)`.

`rmtree` deletes entries by bare name against a directory file descriptor
(`os.unlink(entry.name, dir_fd=topfd)`). The substrate patches `os.unlink` and
resolves the candidate through `Path.resolve()`, which **follows the symlink**
onto the real `.venv` inside the open workspace. That is a tracked path, so the
patch demanded a scope:

```
UnscopedMutationError: Workspace mutation 'os.unlink' for .venv requires an
active scope. Wrap edits in fork()/merge() before mutating the workspace.
```

`UnscopedMutationError` derives from the substrate's error base, **not** from
`OSError`, so `ignore_errors=True` did not swallow it and the exception left
teardown and left the run.

**Blast radius, established by probe** (workspace open, cwd at the repo root,
four stage shapes torn down):

| stage holds | in-process rmtree |
| --- | --- |
| real files only | clean |
| a file whose bare name also exists at the repo root | clean |
| a symlink resolving **into** the workspace | `UnscopedMutationError` |
| a symlink resolving outside the workspace | clean |

So the trigger is the symlink, not the file name and not the working directory.
An earlier reading of this defect blamed a bare name resolved against the cwd;
that is wrong — `_resolve_path_with_dir_fd` honours `dir_fd`, and the
`name_collision` row above refutes it. The affected repos are the ones where
`_link_dep_dirs` has something to link: a `.venv` or `node_modules` at the root,
which is most Python and most Node repos. Both teardown sites were affected —
the warmed `LocalGateStage` and the plain gate's temporary directory, whose
`TemporaryDirectory` cleanup is the same in-process `rmtree`.

**Fix:** `_remove_tree` deletes a staged tree from a child interpreter, which
carries none of the patches. Pinned by `tests/test_gate_teardown.py`, including
one case against the real substrate in a throwaway workspace; reverting the fix
turns those tests red with the error above.

Verified on darwin and on Linux. The simulated guard resolves a `dir_fd` the
way the substrate does — `F_GETPATH` on darwin, the `/proc` fd link on Linux —
and one test pins that resolution by itself, so a platform where the probe
cannot resolve one fails by name instead of quietly turning the rest of the
file into tests that raise nothing and prove nothing.

### A reworded finding closed as `fixed` without being fixed

**Was:** the ledger keys a finding on the sha256 of its normalized text, and
normalization strips a leading severity label and collapses whitespace —
nothing else. So two reviews describing one problem in different words were two
different findings, and `record_round` closed the earlier one as `fixed` for the
only reason it knew: the reviewer had not raised *that string* again.

The reviewer is a language model; it rewords by default. And nothing told round
2's reviewer what round 1 had objected to, so it re-derived the objection in
fresh words — the rewording was forced by the design, not model caprice.
Observed in a run whose round 2 re-raised round 1's blocking objection verbatim
in substance:

```
[fixed] CONVENTIONS.md "Each module ships its own test file": new module
        duration.py is not accompanied by a test file …   (round 1)
[open]  BLOCKING — CONVENTIONS.md 'Each module ships its own test file': new
        module duration.py is not covered by tests.       (round 2)
```

**Blast radius, established:** the run's *decision* was unaffected. Approval
comes from the reviewer's verdict, and the rework gate is `has_open()`, which
stayed true because the reworded finding was itself open. What was wrong was the
tally's account of history — `[fixed]` on something nobody fixed — and the
`rounds` trail, which exists precisely so a human can see whether an item was
addressed once or kept coming back. A chronic finding read as one closed item
plus one new one. That is the failure the module's own docstring says it exists
to prevent.

**Fix: closure on evidence, never on silence.** Matching by meaning was rejected
outright — a similarity threshold loose enough to catch rewording will
eventually fold two genuinely different findings into one and close a real
problem in silence, which is strictly worse than a tally that over-reports
closure. So identity is not guessed. Each round is handed the still-open
findings with their ids (`Ledger.guidance()` into the review prompt's
`findings`), and the verdict answers about them: an item still present is
re-raised with its id in leading brackets, an item checked and found gone goes
in the verdict's new `resolved` list. `record_round` closes a finding only on an
explicit `resolved`, or on approval — the one judgement that covers the whole
change. A rejecting reviewer that merely stopped mentioning something closes
nothing. An unrecognised id falls back to text identity, so a hallucinated
reference cannot mint a finding keyed on the hallucination, and a finding named
in both `issues` and `resolved` stays open, since only one of the two readings
can hide a real problem.

Pinned by `tests/test_ledger.py` (`ClosureNeedsEvidenceTests`, which holds the
observed pair above verbatim) and `tests/test_review_rounds.py`
(`TheReviewerSeesTheOpenFindings` — that the prompt teaches the id protocol,
that the task accepts `findings`, that the open findings actually reach the
reviewer, and that `resolved` is read off the verdict).

### The attempt counter read as a per-round budget and was not one

**Was:** a rework round resets `attempts_left` to `max_attempts` — a round
carries its own allowance, by design — but `number`, the counter in the
`attempt N/M` label, is monotonic for the whole run and never resets. So the
second round's first attempt printed `attempt 2/2` while in fact holding a full
fresh allowance, and a run reaching a third round would have printed
`attempt 3/2`. Display only: the budget arithmetic was correct, and no attempt
was lost or gained. It was the label that described the wrong quantity.

**Fix:** `_attempt_label` numbers the attempt by its position within the round
it is actually spending — `max_attempts - attempts_left` — and names the round
whenever there has been more than one. A single-round run prints exactly what it
printed before. The gate and review lines, which used to print a bare
`attempt {number}` with no denominator, now carry the same label as the worker
line: adjacent lines describing one attempt with different numbers is how the
counter became unreadable in the first place.

Pinned by `tests/test_review_rounds.py` (`TheAttemptLabelNamesItsOwnBudget`).
One of those tests drives `develop()` through a real rework round with a
capturing reporter and asserts the old string `attempt 2/2 · worker running` is
absent — pinning the pure helper alone would have let the call sites be reverted
to the run-wide counter without a test noticing.

### `shepherd-dev optimize` crashed on every run that reached a proposal

**Was:** `KeyError: 'guidance_review'`, uncaught, from
`{k: DEFAULT_PROMPTS[k] for k in EDITABLE_KEYS}` in `optimize._propose`. The
command was dead for anyone who invoked it.

The prompts live in two copies. shepherd-ai's single-file task capture rejects
any same-package import in a task source, so `tasks.py` cannot import
`prompts.py` and carries an inlined duplicate, kept in sync by a docstring
asking politely. `guidance_review` was added to `prompts.py` and to
`optimize.EDITABLE_KEYS`, and not to `tasks.DEFAULT_PROMPTS` / `PROMPT_KEYS`.
`optimize` imports the `tasks` copy. Beyond the crash, `save_overrides` filters
by `tasks.PROMPT_KEYS` and drops anything else without a word, so even a fixed
`_propose` would have thrown a winning candidate away while reporting success.

**Fix:** the missing key added to both `tasks.PROMPT_KEYS` and
`tasks.DEFAULT_PROMPTS`, with the prompt text copied byte-for-byte from
`prompts.py`. Pinned by `tests/test_prompt_copies.py`, which is the mechanism
the polite docstring was standing in for: the two copies must agree key for key
and byte for byte, and every key `optimize` offers to edit must be both readable
from `DEFAULT_PROMPTS` and survivable through `save_overrides`.

### A hosted proposal reverted the human's edits

**Was:** editing a file while a `--provider grok` / `--provider codex` run was
in flight, then settling, silently restored the file to what it had been before
the edit. No conflict, no warning: the worker had never touched that file.

`collect_changed_entries(repo_root, clone)` compared the worker's clone to the
repo AS IT IS WHEN THE WORKER FINISHES. So the base moved under the comparison:
a file the human edited mid-run now differs from the clone's untouched copy,
which reads as "the worker changed this" and pulls it into the proposal
carrying the PRE-RUN content. Settling then writes that over the edit.

Hosted proposals also carried no `regate_cmd`, so `settle-par` skipped the
settle-time re-gate — the one check that catches a base that moved was missing
from exactly the path that races the human.

**Fix:** `snapshot_tree(clone)` pins the base before the worker starts, and the
diff is taken against that snapshot, so only files the worker actually wrote
can enter the changeset. `regate_cmd` is emitted. Pinned by
`tests/test_grok_host.py`, whose `FakeGrokExecutor.on_run` hook edits the real
repo mid-run; without the fix the edited file appears in the changeset with its
stale bytes.

### The remote gate adopted a warmup that was still staging

**Was:** with a stateful remote `setup_cmd` (a DB, containers), a run could
bring the same per-run state up twice at once, and overlay a proposal onto a
half-copied tree.

`GateWarmup.join()` waited `self.timeout`, but `_stage` makes up to TWO remote
calls bounded by that same timeout — real worst case is 2x. When the join gave
up early the thread was still copying, and `error` was still `None` because
nothing had failed YET. The caller read `error is None` as "staged and
healthy". `did_setup` was likewise still `False`, so `run_remote_gate` ran
`setup_cmd` alongside the warmup's own still-running one.

The lesson generalises past this call site: **the absence of an error is not
evidence of completion.** Anything that reports progress by mutating fields
from a thread needs a separate "finished" signal.

**Fix:** `_stage` sets a `_done` Event in a `finally`, `join()` waits against a
deadline covering the true staging budget and RETURNS whether staging finished,
and `_adoptable()` is the single predicate the gate consults. `teardown()` no
longer joins inline — removing a workdir ssh is still writing into is the other
half of the same race. Pinned by `tests/test_remotegate_warmup.py`
(`WarmupStillStagingIsNotAdopted`).

### The gate judged the proposal under shepherd's own environment

**Was:** the suite passed or failed depending on who ran it. Same commit, same
machine: green with a clean shell, two failures with a `PYTHONPATH` pointing
elsewhere.

`run_streaming` spawned the test command with no `env=`, so the gate inherited
`os.environ` verbatim. The gate exists to judge the PROPOSAL's materialized
tree, but `PYTHONPATH` puts shepherd's own source on the suite's `sys.path`,
`VIRTUAL_ENV`/`PYTHONHOME` redirect the interpreter at our checkout, and
`PYTEST_CURRENT_TEST`/`PYTEST_ADDOPTS` leak the state of whatever pytest run
launched us into the child's.

**Fix:** `gate_env()` removes that interpreter and harness state and nothing
else. The strip list is deliberately narrow — a blanket scrub would take
`PATH`, `HOME` and the project's own configuration with it, which is what every
real gate needs. `SHEPHERD_DEV_GATE_ENV_KEEP` re-admits an entry;
`SHEPHERD_DEV_GATE_ENV_STRIP` drops more. Pinned by
`tests/test_gatestream.py` (`GateEnvIsolation`), which asserts both halves: the
interpreter state is gone AND an ordinary variable still arrives.

### A diff optimization made the worker's edit disappear

**Was:** `tests/test_diffcollect.py` failed intermittently on Linux and never on
macOS. Underneath the flake: a worker's change could be dropped from the
changeset in silence.

A `(size, mtime)` fast-path dismissed a file as untouched without reading it.
But Linux stamps mtime from a coarse clock — one jiffy, 1-4ms — so a same-size
rewrite landing in the tick the snapshot was taken in carries a byte-identical
`(size, mtime)` pair. APFS timestamps are fine-grained enough that it never
reproduced on the machine it was written on.

Two things are worth keeping from this. First, it is the same
disappearing-work failure the hosted-diff entry above exists to prevent,
reintroduced as a performance tweak. Second, **a racy-window margin cannot
rescue it**: a freshly made clone's files are milliseconds old at snapshot time
— measured at 4.6ms — so every one of them falls inside any sound margin and
gets hashed anyway. The optimization is worth nothing once it is correct.

**Fix:** removed; comparison is by content hash only. Pinned by
`test_a_rewrite_of_the_same_size_on_the_same_mtime_is_still_detected`, which
forces the collision with `os.utime` rather than racing the clock, so it is
deterministic on every filesystem.

### The suite had never run anywhere but a laptop

**Was:** the defect above reached `main` because nothing else could have caught
it. The only workflow was `release.yml`, gated on `v*` tags, and the single tag
in the repo predates it — so no CI had ever run. 443 tests existed and were
executed only by whoever last touched the code, on whatever filesystem they
happened to have.

**Fix:** `.github/workflows/ci.yml` on every push and pull request, across
ubuntu (coarse mtime, GNU coreutils) and macos (nanosecond APFS, BSD userland),
on the 3.11 floor and a current interpreter. The matrix is the point, not
decoration. A second job re-runs the timing-sensitive modules ten times, which
is the cheap version of the loop that actually found the flake.

### Hosted workers orphaned their CLI's subprocesses

**Was:** a `grok` or `codex` worker that overran its budget left node processes
and MCP servers running — holding CPU, memory and API sessions with nothing
left to reap them.

`subprocess.run(timeout=...)` kills only the DIRECT child. The claude path had
solved this twice over — the launch perl puts the runner in its own session and
kills the process GROUP at the budget (#A), with the watchdog as a second layer
(#B) — and the hosted path, extracted later, inherited neither.

**Fix:** `run_cli_worker` routes both executors through
`procstream.run_streaming`, which already does `start_new_session` + `killpg`.
It lives in `hosted.py`, the provider-agnostic layer, so a provider added later
gets the guarantee instead of repeating the bug. Pinned by
`tests/test_hosted_killtree.py`, whose stand-in CLI spawns a detached
grandchild and reports its pid.

### Two commands on one repo could delete each other's workspace store

**Was:** unreproducible failures when a `run` and a `settle` overlapped, or two
runs started together.

`_refresh_substrate` rmtree's `.vcscore` and re-inits it, taking no lock. Any
concurrent reader — a settle listing runs, another run deciding whether it may
reuse the adoption — could find the directory disappearing under it, and two
runs could rmtree each other's fresh adoption.

**Fix:** `substrate_lock`, an advisory flock held across the whole sequence, so
reading the pending state, deciding on the key, the rmtree and the re-init are
one transaction. `settle_run` takes it too: locking only the writer leaves the
race intact, since the reader consumes from exactly the store the writer wipes.
The lock file lives BESIDE `.vcscore` — the file that survives the rmtree is the
only one that can guard it — and is gitignored by `init`. Best effort by
design: no `fcntl`, an unwritable repo, or a lock held past the timeout all
proceed anyway, degrading to the old behaviour rather than to a hang. Pinned by
`tests/test_substrate_lock.py`.

### `alarm` was armed before its handler, and a zero budget disabled it

**Was:** two defects in the launch perl, both around the same line.

The group-kill handler was installed only after `fork`/`pipe`, leaving a window
in which the timer was armed and `SIGALRM` still carried its DEFAULT
disposition: the process dies bare, with no group kill and no exit 124.

Separately, `--worker-budget 0` did not, as it reads, kill the worker at once.
Perl's `alarm 0` CANCELS the timer. The budget was silently NOT enforced, and
since the watchdog keys off the same number, nothing was left supervising the
run's wall clock.

**Fix:** arming before `pipe`/`fork` is load-bearing — the timer survives
`exec`, so the degenerate fallbacks still die at the budget — so the handler
moved ahead of the arming rather than the arming moving back. `$pid` is
declared up front and the closure checks definedness. A non-positive budget is
refused at the CLI and in `set_worker_budget`. Pinned by
`tests/test_teepump.py`, which asserts BOTH orderings: handler before arm, arm
before pipe/fork.

### Smaller defects fixed in the same pass

Each of these had a short mechanism and a test; recorded together because none
of them repays a section of its own.

- **A crashing worker escaped as a raw exception.** `run2` and `best-of`
  collected futures with a bare `[f.result() ...]`, which re-raises out of the
  call: no report, and the results of the workers that DID finish discarded
  with it. `develop_many` had contained this since it was written. Later
  clones — the conflict handoff and each repair round — had the same hole and
  were closed with it.
- **The speculative reviewer was never joined on the gate-failure paths.** The
  join sat only on the review path, which both gate failures jump over, so a
  daemon thread stayed inside `run_review` holding the workspace — racing
  `output.discard()` and overlapping the NEXT attempt's `workspace.run`.
- **`settle-par` had no recovery for a partial write.** An `OSError` partway
  through `materialize_into` propagated out with the worktree half-written and
  nothing said. It now keeps the stage (so nothing is lost, the content being
  still on disk) and reports which files landed.
- **Auto-commit deleted the accepted files from the worktree.** Committing them
  onto `shepherd/<slug>` and checking the original branch back out removes
  them, since that is where they now exclusively live — contradicting the
  message telling the user to review them.
- **The adoption fingerprint missed edits inside untracked directories.**
  `git status --porcelain` collapses one to `?? dir/`, and a DIRECTORY's mtime
  does not move when a file inside is rewritten in place. `-uall` lists the
  files themselves.
- **The CRO replay was killed partway through its gate.** The parent timeout
  was `worker_budget + 300`, which does not cover even the gate's default 600s,
  and the bare `except` folded the `TimeoutExpired` into a candidate failure it
  never had.
- **Remote preflight rejected `MIX_ENV=test mix test`.** `shlex.split(...)[0]`
  is the assignment, not the binary, so preflight demanded a command named
  `MIX_ENV=test`.
- **Every remote exit 124 was read as a timeout.** `timeout` propagates the
  command's own status verbatim, so a suite exiting 124 became an
  `infra_error` that aborts the run instead of a retryable gate failure.
  Elapsed time separates the two.
- **`run2` stopped emitting `review.verdict`.** A restructuring left the
  emission under `else:` (no review task), where the verdict is always `None` —
  dead code, and the verbose log silently lost the reviewer's outcome.

### The adoption fingerprint trusted mtime for dirty files

**Was:** `_adoption_key` decides whether a worker may build on the CACHED
adoption instead of re-adopting the worktree. It hashed `(mtime, size)` per
dirty path — the same evidence the diff fast-path above was removed for. On a
coarse-timestamp filesystem a same-size rewrite inside one tick keeps the pair
identical, so the stale adoption is reused and the worker builds on a base that
no longer matches the worktree, silently.

Kept open for a while on the grounds that the window is far narrower here: the
two key computations are whole runs apart, so a collision needs two edits in
the same jiffy in different runs, where the diff case had them milliseconds
apart by construction. That reasoning was sound about the ODDS and beside the
point about the FIX, which turned out to cost almost nothing.

**Fix:** hash the content. Only dirty paths are read — normally a handful —
each streamed in chunks so one enormous file costs time rather than memory.
Content also stops a bare `touch` forcing a multi-second re-adoption, which the
old key did. `_digest_dirty_path` handles what else a dirty path can be: a
symlink folds in its target, a submodule folds in a marker rather than walking
an unbounded tree, and an unreadable file folds in a marker rather than
freezing the key on its last good value — unreadable is not evidence of
unchanged. Pinned by `tests/test_perf.py` (`AdoptionKeyTests`), which forces
the collision with `utime` instead of racing the clock.

Writing the symlink branch surfaced a latent `NameError`: `cli.py` had never
imported `os`.

### The watchdog could signal a reused pid

**Was:** targets are chosen from a `ps` snapshot and signalled afterwards, with
a whole grace period between SIGTERM and SIGKILL. A pid is not an identity —
one freed inside that window goes straight to the next process the OS starts,
and signalling it kills a stranger.

**Fix:** `_kill_subtree` takes the snapshot the pids came from and re-reads
`ps` immediately before each signal, dropping any pid whose parent and command
line no longer match. That narrows the window from the whole kill sequence to a
single `ps` call. Deliberately not proof: a pid could in principle be reused by
an identical command under the same parent, but that is a different order of
unlikelihood from plain reuse, and `pidfd_open` — which would be proof — is
Linux-only while this runs on darwin too. The re-check runs twice, since the
grace is the widest part of the window and a target killed by the SIGTERM
spends all of it releasing its pid. A machine where `ps` fails changes nothing
rather than sparing everything.

### The suite littered the temp root, and the gate leaked its staged tree

**Was:** a full run left 88 directories behind. Litter rather than breakage —
but a stray tree is indistinguishable from one a test meant to leave, which is
how the TMPDIR-deletion defect above went unnoticed.

Chasing the last one found a real leak. `_remove_tree` deletes the staged gate
tree from a CHILD interpreter, deliberately, so the substrate's `os.unlink`
patch is not in play. It spawned that child with the environment inherited
verbatim — and the child is OUR interpreter, so a `PYTHONHOME` or `PYTHONPATH`
aimed elsewhere stops it booting at all. With `check=False` and a bare
`except: pass`, that failure is invisible and every staged tree leaks in
silence.

**Fix:** `tests/tmpdirs.py` wraps `mkdtemp` and removes what it made at
interpreter exit — atexit rather than teardown because most of these are
created in module-level helpers with no TestCase to hang an `addCleanup` on.
The cleanup child now runs under `gate_env()`, which drops exactly the
variables that stop an interpreter starting. What survives a full run is one
`vcs-core-overlay` directory, which belongs to the substrate.

### Every child interpreter shepherd spawns inherited the environment

**Was:** the staged-tree leak above was one site of six. shepherd spawns its
own interpreter once to remove a staged tree, twice to replay a case during
`optimize`, and three times to run `shepherd init` for a fresh clone or a
re-adoption — the `shepherd` console script being a python entry point like
any other.

All of them passed the environment through untouched, and `PYTHONHOME` names a
stdlib prefix: a wrong one stops the child booting *before any of our code
runs*. So the failure never mentions its cause. In `_remove_tree` the
`check=False` hid it completely; in the replay it surfaces as a candidate
scored on a failure of the harness rather than of the prompt (compounding the
replay-timeout defect above); in `shepherd init` it surfaces as
`shepherd init failed` with a message about the repo.

**Fix:** `child_python_env()` at every one of those sites. It drops
`PYTHONHOME` and `__PYVENV_LAUNCHER__` and nothing else.

The narrowness is the point, and the reason worth keeping: **`PYTHONPATH` must
not be stripped here.** `-m shepherd_dev.cli` needs the package importable, and
anyone running from a source checkout gets that from `PYTHONPATH` — stripping
it would trade a rare breakage for a common one. Only a child that imports
nothing but the stdlib can afford the wider `gate_env()` scrub, which is why
the tree remover uses that one and these do not. Pinned by
`tests/test_gatestream.py` (`child_python_env` keeps `PYTHONPATH`, drops
`PYTHONHOME`, and a real child boots under a poisoned one).

### A name read but never bound waited for the first caller to reach it

**Was:** `cli.py` called `os.readlink` in a branch nothing exercised, and never
imported `os`. A `NameError` sitting in the tree, due to whoever first ran with
a symlink among their dirty files. It was found by accident, when a test
written for something else happened to walk that branch.

Coverage is the wrong instrument for this. Tests cannot reach every branch, and
they do not have to: a missing import is not a behaviour that needs observing,
it is a name that either resolves or does not, and that is **decidable without
running anything**.

**Fix:** `tests/test_undefined_names.py` decides it for every module in the
package. The scope analysis comes from `symtable` — the interpreter's own — so
nested functions, comprehensions, class bodies, `global`/`nonlocal`, and the
function-local imports this codebase uses everywhere are handled by Python's
rules rather than by an approximation of them.

Three cases pin that the check can still fail, one of them the exact `cli.py`
defect: run against the file as it was, it reports
`cli.py._digest_dirty_path: os`. A check that cannot fail proves nothing.

### A worker's chmod +x never reached the gate, the tar, or the settle

**Was:** the changeset read carried git's filemode (`read_file` yields
`(bytes, mode)`), and `read_changeset_entries` kept only the bytes. From
there the proposal flowed as `dict[str, bytes]` through the whole pipeline,
so the two write points had nothing to re-apply: `materialize_into` wrote
with the filesystem default and the remote gate's tar never set
`TarInfo.mode`. A worker whose fix included `chmod +x deploy.sh` saw the
gate fail `./deploy.sh` BEFORE the proposal was judged, and an accepted
proposal landed on the real repo as 0o644.

**Fix:** the bytes stay a plain dict — every reader (diff text, policy,
review) keeps working — and the exec bit rides alongside in an `Entries`
subclass (`diffcollect.py`). Both producers fill it: the git lane from the
filemode, the hosted lane from a `(hash, exec-bit)` snapshot, which also
makes a mode-ONLY change visible as a change (content comparison alone
cannot see one). Both writers re-apply: `materialize_into` chmods 0o755,
`_tar_entries` sets the member mode. Copies are the danger point —
`dict(...)` / `{**a, **b}` strip the attribute — so every merge goes through
`as_entries`, and settle-par re-reads the bit from the staged files
themselves (the stage wrote them with the bit already on). Pinned by
`tests/test_diffcollect.py` (`ExecBitTracked`), `tests/test_supervisor.py`
(`ExecBitSurvives`) and `tests/test_remotegate_ssh.py` (`TarCarriesModes`).

### The planning pass obeyed the repository it planned for

**Was:** `plan_targets` ran `claude -p` in the caller's working directory with
none of the worker's hardening. Started from the repository under review, the
CLI loaded that repository's `CLAUDE.md` (and any `CLAUDE.md` in a directory
above it), ran the hooks in its `.claude/settings.json` and started the
servers in its `.mcp.json`. All of this happened in shepherd's own process,
outside the worker's jail. On a toy repository holding all three, 0.1.43
returned the plan the repository's `CLAUDE.md` dictated, including a target
the feature never touched, and both the hook and the MCP server ran their
commands. A benchmark caught the milder form: an owner's personal
`CLAUDE.md` sat above the run directories and reached every planning call.

**Fix:** the call gets the worker's isolation. `HOME`, `CLAUDE_CONFIG_DIR` and
`TMPDIR` point into a fresh scratch, and the host login is seeded there the way
the substrate seeds the worker's (`0600`, removed with the scratch). It carries
`launch.HARDENING_FLAGS` (no MCP servers, no browser bridge, user settings
only) plus `--tools "Read,Glob,Grep"`: the planner still runs in the repository
and may open its files, but cannot change or run anything. The prompt comes
before `--tools` because that option takes a variadic list. The redirect
matters on its own: with the flags alone, the user's own `CLAUDE.md` still
dictated the plan on the same toy repository. With the fix, the plan follows
the feature, neither marker file appears, and nothing is written into the
user's config directory. Pinned by `tests/test_planning.py`
(`test_runs_isolated_from_the_repository`, `test_runs_in_the_repo_when_given_one`,
`test_no_host_login_seeds_nothing`, `test_tempdir_failure_returns_error`).

### A Write over an existing file hung the run

**Was:** with verbose events on (the default), every `Write` tool call over a
file that already existed in the repository deadlocked the run. The stream
tailer renders that call as a diff against the old content, and read the old
content with `read_text()`. The substrate wraps `builtins.open` and `io.open`
and records each read inside the workspace under its runtime lock, which the
launching thread holds for the whole worker launch. The tailer waited for that
lock with its own `_pump_lock` held; the worker went on to finish, with no
events recorded after the `Write`; then `drain()`, on the launching thread,
waited for `_pump_lock` with no limit. Neither thread could move, and the
worker budget did not help: by then there was no worker left to kill. On a
Linux host with a Haiku worker, 3 of 6 runs of an implement task whose stub
the worker rewrote with `Write` stopped this way and were killed by an outer
one-hour timeout. Across every run on that host, no `worker.write` event ever
carried a diff: the path had never completed once. A `faulthandler` dump of
the stuck process showed the main thread in `StreamTailer.drain` and the
tailer thread in the substrate's `record_runtime_effects`.

**Fix:** the old content is read with `os.open`/`os.read`, which the substrate
does not wrap, so the read takes no substrate lock. It is also no longer
recorded as a read the worker made, which it was not. Newlines are still
translated as `read_text()` did. `drain()` now waits at most its timeout in
all, for the thread and its `_pump_lock` together; if the tailer is still
stuck, it records `worker.stream.stuck` and returns, so a wedged observer
costs the events from the stuck line on, not the run. Pinned
by `tests/test_events.py`
(`test_baseline_read_does_not_open_through_the_substrate`,
`test_drain_returns_when_the_tailer_is_stuck`) and
`tests/test_verbose.py` (`test_newlines_translated_like_read_text`).

### On Linux the jailed worker could not write to /dev/null

**Was:** the Landlock jail in the substrate (`vcs_core._landlock_containment`,
shipped in the shepherd-ai wheel) denied every write outside the writable
roots, `/dev/null` included. Every shell redirect into it, `cmd >/dev/null`
or `2>/dev/null`, failed with `Permission denied` and exited before the
command ran. The Claude CLI's own Bash wrapper hits it too, so a worker
command could fail even when it named no `/dev/null`. Seatbelt allows writes
under `/dev`, so macOS never showed it. In a Linux experiment with a Haiku
worker, 26 of 173 Bash calls failed this way, among them a plain
`python3 -m unittest -q 2>&1`.

**Fix:** shepherd-ai 0.3.5 (fork `@713b032`, release
`v0.3.5-landlock-devnull`) adds one Landlock rule on `/dev/null` itself,
opened with `O_PATH` and granting `write_file` only. Nothing else under
`/dev` is granted: `/dev/shm` stays denied. If the rule cannot be added, the
ruleset stays as strict as before. `pyproject.toml` and `uv.lock` now pin
that wheel. Pinned by the fork's
`test_landlock_allows_dev_null_and_nothing_else_under_dev`
(`vcs-core/packages/core/tests/container/test_landlock_containment.py`),
which fails on 0.3.4 with `cannot create /dev/null: Permission denied`; this
suite does not exercise native jails.

### A worker's test could take the whole host's memory

**Was:** the jail confines where a worker writes, not how much memory the
commands it runs may take. In a Linux experiment a Haiku worker rewrote an
inline Markdown parser and ran its own tests; one `python3` grew to 19.8 GB
and the kernel's global OOM killer (`constraint=CONSTRAINT_NONE`) had to pick
a victim among every process on a shared host. It picked the test that time;
nothing in shepherd would have stopped it from picking a service.

**Fix:** opt-in `limits.memory_max` / `--memory-max SIZE` (run, run2, runN).
Each worker and reviewer launch and each local gate and `pre_gate_cmd` run
is wrapped in
`systemd-run --user --scope --collect -p MemoryMax=SIZE -p MemorySwapMax=0`;
the command is exec'd in place, so the killtree and the watchdog see the
same pid and group. Worker and reviewer launches got the scope inside their
argv, after the perl swaps, until the jail started hiding the user bus (next
entry); since then the scope wraps the jail from outside, and what it execs
there may be bubblewrap, which forks the jailed runner and takes it down with
it (`--die-with-parent`). An overrun is killed inside its own scope
(`constraint=CONSTRAINT_MEMCG`). That kill was first measured with the scope
started from inside the Landlock jail; `LimitAroundTheJail` measures the
current arrangement. A limit that cannot be enforced stops the
run before it starts: the preflight starts a real scope and reads back its
`memory.max`, because a scope without the memory controller delegated starts
fine and carries no limit. `--provider grok|codex` is refused with a limit,
since those launches do not go through it; the remote gate is not covered.
The wrapped command gets back the `XDG_RUNTIME_DIR` it had without the
limit: `systemd-run` needs it to find the user bus, and nothing after the
scope should inherit a setting the user did not have.
From systemd 254 on, `systemd-run` may rewrite `${...}` and `$$` in the
command it runs, and that command is a worker prompt or a gate string, so
the scope passes `--expand-environment=no` there. Older versions do not
expand and reject the flag; a version that cannot be read refuses the limit.
On systemd 255 a `--scope` probe left `${HOME}`, `$$` and `$HOME` intact
with and without the flag: there the flag is a guard, not a measured repair.
With no limit set, every argv and gate command is unchanged. Pinned by
`tests/test_memlimit.py`, whose `Live` class runs a real over-the-cap
allocation where the host can enforce the limit.

### A jailed worker could run commands outside the jail through the user bus

**Was:** the Landlock jail confines writes by path, and Landlock below ABI 6
does not govern connecting to a socket by path. `$XDG_RUNTIME_DIR` holds the
user's session bus and systemd's private socket, so a confined command could
run `systemd-run --user -- cmd`. systemd then started `cmd` as its own child,
outside the jail's process tree, and the jail never applied to it. On
Ubuntu 24.04 (kernel 6.8, Landlock ABI 4) a confined `systemd-run --user`
wrote a file outside the writable roots. The memory scope made the path
easier to find, since it set `XDG_RUNTIME_DIR` for the command, but a
worker could guess `/run/user/<uid>` on its own.

**Fix:** shepherd-ai 0.3.7 (fork, release
`v0.3.7-landlock-command-servers`; 0.3.6 at `@2753e96` covered the runtime
directory alone), pinned in `pyproject.toml` and `uv.lock`. Where a socket
directory of a server that runs commands for the user exists (the runtime
directory with the session bus and systemd's private socket, tmux's
`tmux-<uid>`, screen's `S-<user>`), the Landlock backend puts bubblewrap in
front of its runner. bubblewrap keeps the host view and mounts an empty tmpfs
over each such directory, so the sockets are gone before Landlock confines
the runner. A workspace or writable root that sits under one of them is bound
back on top of the tmpfs, where Landlock still governs its writes. The jail
probe checks inside the real jail that nothing else shows. Pinned by the
fork's `test_landlock_blocks_a_write_through_systemd_run`,
`test_landlock_hides_the_user_service_manager_sockets` (both fail on 0.3.5),
`test_landlock_hides_the_tmux_socket_directory` and
`test_landlock_keeps_a_workspace_that_sits_under_a_hidden_directory`. A real
Claude worker ran through it on Ubuntu 24.04 and saw `/run/user/<uid>` empty
and its `bus` missing. shepherd-dev also unsets `TMUX`, `STY`,
`DBUS_SESSION_BUS_ADDRESS` and `DOCKER_HOST` for the worker
(`tests/test_launch_policy.py` `CommandServerAddresses`). What stays open is
under Open, above.

Without a working bubblewrap (not installed, its user namespace denied by
AppArmor, or a trial run that does not finish in 10 s), the worker still runs,
with that path open. So does a run where the user can write to a Docker
socket. The CLI prints one warning at the start naming the paths, the reason
and how to close each (`launch.jail_exposure`, `cli._warn_jail_exposure`),
and silenced the backend's per-launch repeats by their category,
`JailExposureWarning`, not by their text (0.3.9 narrowed that to the repeat
of the text it printed; below). A shepherd-ai older than 0.3.7 is
reported the same way instead of passed over. `--provider codex|grok` runs
under its own CLI's sandbox, which the mask does not reach, and is told so.
Hosts with none of those sockets launch exactly as before.

The memory scope needs the bus, so it moved out of the worker argv:
`memlimit.activate` hands it to the backend's `set_outer_prefix`, which puts
it around the jail. As a result the limit covers every confined launch,
including the framework's own provider when the rebind fails, and the
guards that stopped a limited run on a failed rebind are gone. The jail
probe's own launches skip that prefix: they only touch a file, and a
`systemd-run` failing there would read as a denied write. A host without
Landlock refuses the limit, since no worker launch would go through the jail
and its scope; so does a shepherd-ai older than 0.3.7, and a prefix the
backend does not take leaves nothing active. Pinned by
`tests/test_memlimit.py`: the activation tests, `WorkerArgv`, and
`LimitAroundTheJail`, which, on a host that can do both, launches through
the real jail and reads back the scope's `memory.max` and an empty runtime
directory, and by the fork's `test_landlock_probe_runs_without_the_outer_prefix`.
`tests/test_launch_policy.py` `JailExposure` pins the warning.

shepherd-ai 0.3.8 (release `v0.3.8-jail-socket-masks`) closes what a review
of 0.3.7 found still open:

- **Was:** the runtime directory was hidden only when the bus or systemd's
  private socket was in it, so rootless Docker's and Podman's sockets there
  stayed reachable on a host without a user manager. **Now** it is hidden
  whenever it exists, and the Podman system socket and a `DOCKER_HOST` over
  tcp or ssh are reported.
- **Was:** a tmux server started after the launch created `/tmp/tmux-<uid>`
  beside the mask. **Now** the backend creates that directory (and
  `$TMUX_TMPDIR/tmux-<uid>`) 0700 when absent, so the socket lands under the
  tmpfs. Screen's `/run/screen` is root-owned and is left alone (Open, above).
- **Was:** a path bound back into a hidden directory brought its sockets with
  it: keeping the runtime directory's `systemd` as a writable root brought
  `systemd/private` back. **Now** each socket inside a bound-back path gets
  `/dev/null` bound over it, read-only.
- **Was:** only the default socket directories were hidden. **Now** the
  socket `$DBUS_SESSION_BUS_ADDRESS` (`unix:path=`) or `$TMUX` names is masked
  on its own with `/dev/null`, not its directory, which can be `/tmp` itself
  where no systemd runs (`/tmp/dbus-XXXX`); hiding that would empty `/tmp`
  for the worker.
- **Was:** the probe stopped the run when its check inside the jail failed.
  **Now** a check that still finds an entry or a socket reachable marks
  hiding as failed for the process: the launches that follow run without
  bubblewrap, with a warning that names what it found, as on a host where
  bubblewrap cannot start.
- **Was:** on macOS the Seatbelt profile let a confined command connect to the
  user's tmux server, measured on macOS 27.0.1 with a Unix socket server in
  `/tmp/tmux-<uid>`. **Now** the profile adds `(deny network-outbound
  (subpath ...))` for tmux's and screen's directories (realpaths, since
  Seatbelt does not resolve `/tmp` to `/private/tmp`) and `literal` for the
  socket `$TMUX` or the bus address names. Unix sockets as a whole stay
  allowed: name resolution goes through mDNSResponder's socket. On the same
  host `launchctl submit` from inside the sandbox did not start the job; that
  measures this one launchd route, not every route through launchd (see
  0.3.10, below). `exposure` reports a writable Docker socket (Docker
  Desktop, Colima, OrbStack) and a network `DOCKER_HOST`, and shepherd-dev
  prints it at the start of a run as on Linux.

Pinned by the fork's `test_landlock_hides_the_runtime_dir_without_a_bus`,
`test_landlock_creates_an_absent_tmux_directory_private`,
`test_landlock_masks_a_socket_inside_a_bound_back_path`,
`test_landlock_masks_a_session_bus_named_elsewhere_and_not_its_directory`,
`test_landlock_hides_both_tmux_directories_and_masks_the_socket_tmux_names`,
`test_landlock_probe_degrades_when_the_hiding_check_fails`,
`test_landlock_reports_docker_host_over_the_network`, and on macOS
`test_seatbelt_refuses_a_connect_to_a_tmux_socket` (`refused` for the tmux
socket, `connected` for one beside it) and
`test_seatbelt_reports_docker_reachable`.

shepherd-ai 0.3.9 (release `v0.3.9-jail-socket-ownership`) fixes two things
the review of 0.3.8 found:

- **Was:** a socket directory was taken by its realpath, so a symlink another
  user planted at `/tmp/tmux-<uid>` (shared `/tmp`) put the tmpfs over
  whatever it pointed at, a directory of this user's included, and on macOS
  the deny rule went there too. **Now** a directory counts only when `lstat`
  shows a real directory this user owns, with its parent resolved and the last
  component not followed; tmux and screen refuse such a directory for the same
  reason. The named sockets get the same check. On macOS an absent directory
  keeps its rule, since a server started later creates it there.
- **Was:** every launch walked each path bound back into a hidden directory to
  find its sockets, and the probe's check walked it again inside the jail; a
  workspace there can be a large tree. **Now** the walk runs once per process
  per path, and the check reads the resulting list. The limit this leaves is
  under Open, above.

Pinned by the fork's
`test_landlock_does_not_follow_a_symlink_planted_at_the_tmux_directory`,
`test_landlock_skips_a_directory_another_user_owns`,
`test_landlock_does_not_follow_a_symlink_named_as_the_tmux_socket`,
`test_landlock_walks_each_kept_path_once`,
`test_seatbelt_denies_nothing_through_a_symlink_planted_at_the_tmux_directory`
and `test_seatbelt_skips_a_directory_another_user_owns`.

shepherd-ai 0.3.10 (release `v0.3.10-jail-socket-revalidate`) fixes what the
review of 0.3.9 found:

- **Was:** the list of sockets inside a bound-back path was kept for the
  process, and bubblewrap creates a missing mount destination on the host: a
  socket deleted after the first launch left an empty file (and its
  directories) in the user's tree at the next launch, measured on Ubuntu
  24.04. **Now** each listed entry is checked again on every launch and
  dropped when it is no longer a socket, and the path is walked again when
  its mtime changes.
- **Was:** the check inside the jail read the list the host built, so it
  could not see a socket the list missed. **Now** it walks the kept paths
  itself, once per process for each set of hidden and kept paths.
- **Was:** 0.3.9 refused every symlink at a socket directory, the user's own
  `/tmp/tmux-<uid>` pointing at a private directory included, and said
  nothing about what it left unmasked. **Now** a symlink this user owns is
  followed when its target is also this user's and of the right kind, and
  `exposure` names every candidate left unmasked.
- **Was:** a setuid bubblewrap (no user namespace) left the host's PID
  namespace in place, so a confined command could read a hidden directory
  through another process's root, `/proc/<pid>/root`. **Now** bubblewrap runs
  with `--unshare-pid`. (0.3.10 also had the check inside the jail try the
  hidden directories through the parent's `/proc/<pid>/root`; with a fresh
  `/proc` that pid never exists there, so that check could not fail. 0.3.12
  replaced it, below.)
- **Was:** an X display the user can reach (a socket in `/tmp/.X11-unix`, or
  `DISPLAY` over the network) lets a command type into another window, and an
  sshd on loopback lets it log in with the user's key, both outside the jail.
  **Now** `exposure` reports both, and shepherd-dev unsets `DISPLAY` and
  `XAUTHORITY` for the worker and refuses them in `jail_env` and `env_keep`.

shepherd-ai 0.3.11 (release `v0.3.11-ssh-login-measured`) fixes the ssh report.
**Was:** 0.3.10 reported "an sshd on loopback that takes the user's key" from
the conditions alone (sshd answering on port 22, an `authorized_keys`, a
private key or an agent). On an Ubuntu 24.04 host with all three, `ssh -o
BatchMode=yes localhost` was refused (`Permission denied (publickey,password)`),
so the warning claimed a login that did not happen. **Now** the check logs in
with `BatchMode=yes` and runs `true`, without writing to the user's
`known_hosts`, and reports only when that succeeds; the result is kept for the
process, so a run tries at most one login. 0.3.12 changed how it logs in
(below).

On macOS, `open -a Terminal x.command` from inside the sandbox did not run the
script on macOS 27.0.1, while the same command outside the sandbox did; no
Seatbelt rule was added for it.

Pinned by the fork's
`test_landlock_drops_a_listed_socket_that_is_gone`,
`test_landlock_walks_each_kept_path_once_while_it_holds`,
`test_landlock_probe_walks_kept_paths_the_listing_missed`,
`test_landlock_probe_walks_kept_paths_once_per_process`,
`test_landlock_follows_this_users_own_link_to_its_own_directory`,
`test_landlock_does_not_follow_another_users_link_at_the_tmux_directory`,
`test_landlock_does_not_follow_another_users_link_named_as_the_tmux_socket`,
`test_landlock_skips_a_directory_another_user_owns`,
`test_landlock_reports_what_it_leaves_unmasked`,
`test_landlock_jail_has_its_own_pid_namespace`,
`test_landlock_reports_a_reachable_x_display`,
`test_seatbelt_follows_this_users_own_link_to_its_own_directory`,
`test_seatbelt_denies_nothing_through_another_users_link_at_the_tmux_directory`
and `test_seatbelt_skips_a_directory_another_user_owns`, and by
`tests/test_launch_policy.py` `CommandServerAddresses` and
`SocketVariablesAreNeverKept`.

On the shepherd-dev side:

- **Was:** a repo could set `XDG_RUNTIME_DIR`, `TMUX_TMPDIR`, `SCREENDIR` or
  the server addresses through `jail_env`, and those values are in this
  process's environment when the backend decides what to hide; `env_keep`
  could keep the addresses the scrub removes. **Now** `JAIL_ENV_REFUSED`
  includes `launch.JAIL_SOCKET_ENV`, and `LaunchPolicy.keeps` never keeps
  those names. Pinned by `tests/test_jail_env.py`
  `test_the_command_servers_socket_variables_are_refused` and
  `tests/test_launch_policy.py` `SocketVariablesAreNeverKept`.
- **Was:** the CLI silenced every `JailExposureWarning` once it had printed
  its own, so a later, different finding (the probe above) was dropped.
  **Now** it silences only the repeat of the text it printed. Pinned by
  `test_cli_warns_once_and_silences_only_the_repeat_of_that_text`.
- **Was:** `runN` set the memory scope and checked exposure before applying
  the repo's `jail_env`, unlike run and run2. **Now** the whole command runs
  inside it. Pinned by `tests/test_jail_env.py` `test_runN_applies_it`.
- **Was:** a launch whose provider could not be built fell back to the
  framework's own without a word, losing the tool denylist, settings
  isolation, env scrub, private `TMPDIR`, role model and process-tree kill.
  **Now** it says so once per process, and the failed-rebind warning names the
  same losses. Pinned by `PlainLaunchFallback`.
- **Was:** a backend whose exposure check raised would have stopped the
  command. **Now** the failure is printed as the warning and the run goes on
  (`test_a_backend_that_fails_its_check_is_reported_and_the_run_goes_on`).

shepherd-ai 0.3.12 (release `v0.3.12-jail-exposure-reports`) fixes what the
review of 0.3.11 found:

- **Was:** the Docker report checked only the system sockets and
  `DOCKER_HOST=unix://`. Docker Desktop for Linux, Colima, OrbStack and Rancher
  Desktop reach the daemon through a docker context or a socket under the home
  directory, and a daemon on plain TCP at `127.0.0.1:2375` takes no socket at
  all; the worker reached each of them unreported. **Now** the report also
  reads the docker contexts (`$DOCKER_CONFIG` or `~/.docker/contexts`), the
  per-user sockets those tools create, a context on loopback TCP, and port 2375
  on loopback when it answers `GET /_ping` with an `Api-Version` header. Linux
  and macOS share the check (`_jail_exposure.py`). Pinned by
  `test_docker_contexts_and_per_user_sockets_are_candidates`,
  `test_docker_daemon_on_loopback_tcp_is_found`,
  `test_a_port_that_is_not_docker_is_not_reported`,
  `test_landlock_reports_a_docker_context_socket` and
  `test_seatbelt_reports_a_docker_context_and_an_ssh_login`.
- **Was:** the ssh check logged in with `StrictHostKeyChecking=no`, so whatever
  answered on port 22 counted as this host, and the user's `~/.ssh/config`
  could add a `ProxyCommand`, a `LocalCommand` or forwarding to the attempt.
  It tried only port 22 on loopback. **Now** it pins this host's own keys
  (`/etc/ssh/ssh_host_*_key.pub`) in a temporary known-hosts file under a
  fixed alias, overrides the options that run or forward anything, asks for no
  session (`-N`), and counts the login when ssh reports it authenticated. The
  ports and addresses come from `sshd_config` (`Port`, `ListenAddress`,
  `Include`; a wildcard address is tried on loopback). With no host keys or no
  listener it does not try. Pinned by
  `test_ssh_login_counts_when_ssh_says_it_authenticated`,
  `test_ssh_login_refused_is_not_reported`,
  `test_ssh_login_that_never_answers_is_stopped`,
  `test_ssh_login_is_not_tried_without_host_keys_or_a_listener`,
  `test_sshd_endpoints_follow_port_listen_address_and_include`,
  `test_sshd_endpoints_default_to_loopback_port_22` and
  `test_landlock_reports_an_ssh_login_to_this_host`.
- **Was:** macOS reported no ssh login, though with Remote Login on and
  network allowed the sandbox reaches it the same way. **Now** it does.
  Measured on macOS 27.0.1: the user's key logged in to its own sshd, and the
  report named it. Pinned by
  `test_seatbelt_reports_a_docker_context_and_an_ssh_login`.
- **Was:** a uid with no passwd entry made the ssh check raise `KeyError`
  (seen on a macOS host whose tests fake the uid). **Now** the check gives up
  without a report. Pinned by
  `test_ssh_login_is_not_tried_for_a_uid_without_an_account`.
- **Was:** the check inside the jail looked for the hidden directories through
  the host's `/proc/<pid>/root`. bubblewrap runs with `--unshare-pid` and a
  fresh `/proc`, where that pid never exists, so the check always passed.
  **Now** the jail compares its own PID namespace with the launcher's and fails
  when they are the same. Pinned by
  `test_landlock_hiding_check_fails_in_the_launchers_pid_namespace` and
  `test_landlock_jail_has_its_own_pid_namespace`.
- **Was:** a D-Bus `unix:path=` with percent escapes, which the D-Bus spec
  allows, named a path that does not exist, so the real bus was neither masked
  nor reported; a bus at `unix:abstract=` was not reported at all. **Now** the
  path is decoded before it is masked, and an abstract bus is reported, on
  both platforms. Pinned by
  `test_dbus_addresses_split_paths_from_abstract_names_and_decode_them`,
  `test_landlock_decodes_a_percent_escaped_bus_path` and
  `test_landlock_reports_a_session_bus_on_an_abstract_socket`.
- **Was:** the runner kept the user's controlling terminal. Where the kernel
  still allows `TIOCSTI` (`dev.tty.legacy_tiocsti=1`), a confined command could
  type into the shell outside the jail. **Now** the runner gives up the
  terminal (`TIOCNOTTY`) after confining itself, unless it leads its own
  session. Pinned by `test_landlock_runner_drops_the_controlling_terminal`.
- **Was:** the Landlock rule struct was a ctypes `Structure` with `_pack_`,
  which Python 3.14 deprecates. **Now** it is packed with `struct`. Pinned by
  `test_landlock_imports_without_a_deprecation_warning`.

On the shepherd-dev side:

- **Was:** with `worker.harden: false` the scrub list was empty, so `TMUX`,
  `STY`, `DBUS_SESSION_BUS_ADDRESS`, `DOCKER_HOST`, `DISPLAY` and `XAUTHORITY`
  reached the worker. **Now** those are unset with or without hardening.
  Pinned by `test_a_disabled_policy_still_unsets_the_command_servers_addresses`.
- **Was:** `TMPDIR` was not among the socket variables, and the macOS jail
  builds the screen socket deny rule from it, so a repo's `jail_env` could
  move that rule. **Now** it is refused like the others. Pinned by
  `test_the_command_servers_socket_variables_are_refused`.
- **Was:** `jail_env` is in this process's environment for the whole command,
  so a `PATH` set there chose programs that run on the host: the `bwrap` and
  `ssh` of the jail, the `env`, `systemd-run` and `sh` that hold the memory
  ceiling, and the `git` that reads the repo, at the start-of-run checks and at
  every launch. **Now** they are found on the user's own `PATH` before
  `jail_env` or `jail_seed` is applied, and those paths are used for the rest
  of the run (`programs.py`; shepherd-ai 0.3.13 for `bwrap` and `ssh`).
  `systemd-run` and `bwrap` missing there stay missing (the ceiling is refused,
  the jail warns); `env`, `sh` or `git` missing there are looked up at exec
  time as before (0.3.14 refuses them instead, below). `PATH` still reaches the worker and the gate. Pinned by
  `test_the_jails_programs_are_found_before_the_repos_path_applies`,
  `test_a_seed_alone_also_finds_them_first`,
  `test_a_program_missing_from_the_users_path_stays_missing`,
  `test_without_jail_env_nothing_is_resolved_ahead` and `test_path_is_accepted`.
- **Was:** `jail_env` could set `LD_*` or `DYLD_*`, which load code into
  `bwrap` and the jail's runner whatever path they are found at. **Now** they
  are refused. Pinned by `test_the_dynamic_loader_variables_are_refused`.
- **Was:** a refused `jail_env` or `jail_seed` key was dropped without a word.
  **Now** the run says which key it ignores and why. Pinned by
  `test_each_ignored_key_is_named_with_the_reason`.
- **Was:** the CLI silenced its printed warning with a prefix match, so a later
  warning that began with the same text was also dropped. **Now** the match is
  anchored at the end. Pinned by
  `test_a_longer_warning_that_starts_with_the_printed_text_still_shows`.
- **Was:** the note that codex and grok are not covered by shepherd-dev's
  socket mask was printed only on Linux. **Now** macOS says the Seatbelt rules
  do not cover them either. Pinned by
  `test_other_providers_are_told_the_mask_does_not_cover_them`.

shepherd-ai 0.3.14 (release `v0.3.14-jail-keeps-environment`) and the
shepherd-dev side fix what the review of 0.3.13 found. The repo's `jail_env`
and `jail_seed` sit in this process's environment for the whole command, and
everything the process did for itself on the host still read that
environment.

- **Was:** the host's own programs were found and run under the repo's
  values: the planner's and the auth probe's `claude` (with the repo's
  `NODE_OPTIONS`, `CLAUDE_CONFIG_DIR` and `HOME`), the `ssh` of the remote
  gate, `cp`, `git` (with `GIT_CONFIG_*`), `ps`, `security`, `notify-send`,
  `osascript`, and the codex and grok binaries. **Now** `programs.resolve()`
  keeps a copy of the user's environment before the repo's applies, and every
  host-side subprocess gets `programs.host_env()` and finds its binary with
  `programs.which()` on that copy's `PATH`. The codex and grok workers are
  found that way but run with the live environment on purpose: `jail_env` is
  how a repo reaches its worker, and those two have no shepherd jail. Once the copy exists, a program
  missing from the user's `PATH` raises `FileNotFoundError` instead of being
  looked up on the repo's (`programs.argv0`). Pinned by `tests/test_jail_env.py`
  `test_what_runs_on_the_host_gets_the_users_environment_not_the_repos`,
  `test_the_planner_gets_the_users_environment_and_claude` and
  `test_after_resolving_a_program_missing_from_the_users_path_is_refused`.
- **Was:** a kept copy with no `PATH` (shepherd-dev started without one) made
  `programs.which()` pass `path=None` to `shutil.which`, which then read the
  live `PATH`, the repo's under `jail_env`. **Now** it falls back to
  `os.defpath`. Pinned by
  `test_a_kept_environment_without_path_does_not_read_the_live_one`.
- **Was:** an empty or `.` entry in the user's own `PATH` (a trailing colon,
  common in shell profiles) made `shutil.which` search the current
  directory, the repo's when shepherd-dev runs there, so a `git` or `cp`
  checked into the repo ran on the host; for an empty entry it returned the
  bare name, which exec looked up again on the live `PATH`, the repo's under
  `jail_env`. `resolve()` also lacked the `os.defpath` fallback. **Now**
  `resolve()` and `which()` search only the absolute entries of the kept
  `PATH` (`_search_path`). Pinned by
  `test_a_relative_entry_of_the_users_path_is_not_searched`.
- **Was:** the worker's `claude` was looked up by the framework's provider at
  launch, on the repo's `PATH`. **Now** shepherd-dev passes the path it found
  on the user's `PATH` (`claude_path`, shepherd-ai 0.3.14), and the auth probe
  takes one too. Pinned by `test_the_workers_claude_is_the_one_on_the_users_path`
  and the fork's `test_headless_runs_the_claude_path_it_was_given` and
  `test_probe_claude_auth_runs_the_claude_path_it_was_given`.
- **Was:** the preflight read `CLAUDE_CONFIG_DIR` and `HOME` from the live
  environment to find the credentials it judges, and the run records' default
  directory came from the live `HOME`; a repo could make the preflight judge a
  token it chose and move the records. **Now** both read the kept copy.
  Pinned by `test_the_preflight_and_the_run_records_read_the_users_home`.
- **Was:** the jail read what to hide (`XDG_RUNTIME_DIR`, `DOCKER_HOST`,
  `DOCKER_CONFIG`, `HOME` and the rest) from the live environment, and the
  command servers' addresses stayed in it for the whole command, where
  anything the process spawned found them. **Now** `worker_environment()`
  removes `launch.JAIL_SOCKET_ENV` and the ssh agent's variables from the live
  environment for the command and puts them back after; the jail reads the
  copy it keeps in `_jail_exposure.resolve_programs()` (`environ()`,
  `home()`), and `gate_env()` gives the gate the scrubbed values back, so the
  gate runs as before. Pinned by
  `test_the_servers_addresses_are_out_of_the_environment_for_the_command`,
  `test_every_command_takes_the_servers_addresses_out` and the fork's
  `test_resolve_programs_keeps_the_environment_the_checks_read` and
  `test_landlock_hides_what_the_kept_environment_names_after_it_changes`.
- **Was:** a shepherd-ai before 0.3.14, whose jail reads the live environment,
  would hide nothing once those addresses are gone from it. **Now** it counts
  as a jail that does not hide (the warning says "needs 0.3.14 or later", and
  the memory ceiling is refused). Pinned by
  `test_a_jail_that_reads_the_live_environment_is_old`.
- **Was:** the Landlock runner was started with `python -m`, which imports
  from the working directory and obeys `PYTHONPATH`: a `vcs_core/` package
  planted there ran in place of the runner, before Landlock (reproduced: the
  runner failed with "No module named vcs_core._landlock_containment" and the
  planted package's marker appeared). **Now** the runner and the in-jail
  checks run with `-I`. Pinned by the fork's
  `test_landlock_runner_ignores_python_settings_and_the_working_directory`.
- **Was:** `jail_env` and `jail_seed` could set `PYTHONPATH` and the other
  `PYTHON*` variables beyond the listed ones, `GCONV_PATH` and `LOCPATH`
  (glibc loads code they name), `SSH_AUTH_SOCK` and `SSH_AGENT_PID`,
  `DOCKER_CONFIG`, and any `SHEPHERD_DEV_*` variable, which configures
  shepherd-dev itself (`SHEPHERD_DEV_CODEX_SANDBOX=danger-full-access` took the
  codex worker out of its sandbox, `SHEPHERD_DEV_GATE_ENV_KEEP` chose what the
  gate inherits). **Now** each is refused and named on stderr with the reason.
  `SSH_AUTH_SOCK` and `SSH_AGENT_PID` are also unset for the worker with or
  without hardening (`test_a_disabled_policy_still_unsets_the_command_servers_addresses`).
  Pinned by `test_every_python_variable_is_refused`,
  `test_the_glibc_loader_variables_are_refused`,
  `test_the_ssh_agent_and_the_docker_config_are_refused`,
  `test_shepherd_devs_own_options_are_refused` and
  `test_each_ignored_key_is_named_with_the_reason`.
- **Was:** the suite runs hundreds of commands in one process, and the first
  one to resolve kept its environment and programs for every later test: a
  test that set its own `SHEPHERD_DEV_RUNS_DIR` read the developer's real run
  records (`test_trace_last_renders_timeline`, measured on a host with
  recorded runs). **Now** every test class that runs a command or resolves
  starts from `tests/hostprograms.fresh_programs`, which clears both caches
  (shepherd-dev's and shepherd-ai's) and restores them after; a run of every
  test module in one process leaves neither set.
- **Was:** `test_gatestream`, `test_remotegate_ssh` and
  `test_remotegate_warmup` replaced `remotegate._ssh_base` with a fake in
  `setUp` and never put it back, so any later test of the remote gate's ssh
  got the fake. **Now** each restores it in its cleanup.

shepherd-ai 0.3.15 (release `v0.3.15-jail-hides-the-agent`) and the
shepherd-dev side fix what the review of 0.3.14 found.

- **Was:** four host-side launches still read the live environment, where the
  repo's `jail_env` sits and from which `worker_environment` has removed the
  ssh agent: `memlimit`'s `systemd-run --version` and scope probe, the remote
  gate's streamed test step, and the codex reviewer. The remote test step
  lost `SSH_AUTH_SOCK`, so for a user whose key is held only by the agent ssh
  exited 255 and the gate reported a test failure, not an infrastructure
  error, on every attempt. The codex reviewer got the repo's `NODE_OPTIONS`.
  **Now** each gets `programs.host_env()` (the reviewer adds `CI`). Pinned by
  `tests/test_memlimit.py` `test_the_probes_run_with_the_users_environment`,
  `tests/test_remotegate_ssh.py`
  `test_the_test_step_keeps_the_users_ssh_agent` and `tests/test_codex_host.py`
  `test_the_reviewer_gets_the_users_environment_not_the_repos`.
- **Was:** on macOS the framework's Claude provider looked up the login on the
  host, unjailed, before each worker and reviewer launch, running `security`
  by its bare name on the live `PATH` and reading `CLAUDE_CONFIG_DIR` and
  `HOME` from the live environment. A repo whose `jail_env` put its own
  `security` first on `PATH` had it run outside Seatbelt on every launch.
  **Now** shepherd-ai resolves `security` with `bwrap` and `ssh`, and the
  lookup reads the environment `resolve_programs` kept. Pinned in shepherd-ai
  by `test_host_login_lookup_runs_the_security_found_before_the_path_changed`
  and `test_host_login_lookup_reads_the_config_dir_and_home_it_kept`.
- **Was:** unsetting `SSH_AUTH_SOCK` for the worker left the agent's socket on
  disk (`/tmp/ssh-*/agent.*`, launchd's `Listeners`). Landlock below ABI 6
  does not govern `connect`, and `(allow default)` in Seatbelt allows it, so a
  worker could find the socket and sign with the user's keys. **Now** the
  jail masks the socket the kept `SSH_AUTH_SOCK` names, as it masks the
  session bus and the tmux server: `/dev/null` over it under bubblewrap, a
  connect deny in Seatbelt; its directory stays visible. Pinned in shepherd-ai
  by `test_landlock_masks_the_ssh_agent_socket_the_environment_names` and
  `test_seatbelt_refuses_a_connect_to_the_ssh_agent`.

shepherd-ai 0.3.16 (release `v0.3.16-jail-reports-agents`) and the
shepherd-dev side fix what the review of 0.3.15 found.

- **Was:** the jail hid only the agent socket the kept `SSH_AUTH_SOCK` named.
  Any other agent of the user's on disk (one an ssh login forwarded to
  `/tmp/ssh-*`, launchd's `Listeners` with `SSH_AUTH_SOCK` unset, gpg-agent's
  ssh socket, 1Password, Bitwarden, Secretive) stayed reachable from inside
  the jail, and the start-of-run warning said nothing about it. **Now** the
  warning names each one it finds at those places that the jail neither masks
  nor hides under a hidden directory. It does not mask them: the report is
  what can be known without guessing each tool's layout, and an agent
  started with `-a` somewhere else is still not found (Open, above). Pinned
  in shepherd-ai by
  `test_agent_sockets_finds_this_users_agents_where_they_put_them`,
  `test_landlock_reports_the_agents_it_does_not_hide` and
  `test_seatbelt_reports_the_agents_it_does_not_deny`.

shepherd-ai 0.3.17 (release `v0.3.17-jail-agents-and-path`) fixes what the
review of 0.3.16 found.

- **Was:** the agent report of 0.3.16 did not look under `~/.ssh/agent`,
  where OpenSSH 10.1 and later and sshd's forwarded agents put their socket
  by default, so it missed the agent most users run (measured: a live agent
  there on macOS with OpenSSH 10.3p1 and `SSH_AUTH_SOCK` unset, not
  reported). It also reported sockets of agents that had died; on Python
  3.11 a link another user planted in `/tmp` ended the listing, and a
  directory removed during it raised out of the report and of the launch
  that called it; and a path with a control character went raw into the
  warning. **Now** it looks under `~/.ssh/agent`, walks one directory at a
  time skipping what it cannot read and never following a link at a
  wildcard, lists a socket unless connecting to it is refused, escapes
  non-printable paths, and a failure to build the report during a launch
  becomes a line of the warning. Pinned in shepherd-ai by
  `test_agent_sockets_finds_this_users_agents_where_they_put_them`,
  `test_agent_message_escapes_a_path_that_is_not_printable` and
  `test_a_launch_warning_survives_an_exposure_that_cannot_be_described`.
- **Was:** on Linux, with no runtime directory of the user's and
  `/tmp/tmux-<uid>` taken by another user, there was no directory to hide,
  so bubblewrap did not run and the session bus, the tmux server and the
  ssh agent the environment names were neither masked nor reported. **Now**
  bubblewrap runs whenever there is a named socket to mask. Pinned by
  `test_landlock_masks_the_named_sockets_with_no_directory_to_hide`.
- **Was:** the fork's `resolve_programs()` found bwrap, ssh and security
  with the same `shutil.which` reading of an empty or `.` `PATH` entry.
  **Now** it keeps only absolute entries. Pinned by
  `test_resolve_programs_skips_relative_entries_of_the_path`.
- **Was:** the last host-side calls were left on the live environment. The
  native gate's `node --version` (`config._node_supports_strip_types`) ran
  the `node` on the repo's `PATH` with its `NODE_OPTIONS`, during
  `resolve_test_cmd`, before any gate or person looked at the run; its
  pytest lookup (`_pytest_available`) read the same `PATH`; and the
  optimizer's `claude` (`optimize._propose`) and its `git worktree add` and
  `remove` (`optimize._replay`) ran with the live environment. **Now** each
  finds its program with `programs.argv0` or `programs.which` and runs
  with `programs.host_env()`. Pinned by
  `test_the_gate_detection_and_the_optimizer_run_on_the_users_environment`.
  Kept by the owner's decision, with its consequence: detection now answers
  for the user's `PATH` while the gate still runs with the repo's. A Python
  repo with no test command configured, no pytest marker, and pytest only
  on a `PATH` its `jail_env` sets gets `python3 -m unittest discover`, which
  collects no pytest-style test: on Python 3.11 the gate passes with none
  run, from 3.12 it fails every attempt. A `node` on that `PATH` older or
  newer than the user's can flip the TypeScript choice the same way. The
  fix on the repo's side is to set `test_cmd`.
- **Was:** on Linux the programs that start the jail (the memory scope's
  `env` and `systemd-run`, `bwrap`, and the Landlock runner) ran on the host
  with the live environment, which held the repo's `jail_env`. Only the list
  of refused names kept a variable from loading code into them or changing
  what they do before Landlock applied. **Now** shepherd-ai 0.3.18 starts that
  chain with the environment `resolve_programs` kept and hands the command's
  own environment to the runner on a memfd, read before it confines itself
  and exec'd with `execvpe`; with no kept environment, or nothing changed
  since, the launch is the single-environment one it was. The refusal list
  stays as a second layer. On macOS `sandbox-exec` execs the command itself
  and has no runner to swap the environment in, so it still starts with the
  repo's values. Pinned by
  `test_the_jails_outer_chain_gets_the_users_environment_and_the_command_the_repos`
  and `LimitAroundTheJail.test_a_repos_settings_reach_the_capped_command_and_not_the_scope`,
  and in shepherd-ai by
  `test_landlock_outer_chain_gets_the_kept_environment_and_the_command_its_own`.

### CI tested against shepherd-ai from PyPI, not the pinned wheel

**Was:** the CI's `test`, `repeat`, `performance` and `static` jobs, and
the release workflow's `test` job, ran `pip install -e .`. pip does not read `[tool.uv.sources]`, so it resolved
`shepherd-ai` from PyPI instead of the fork's wheel that `pyproject.toml` and
`uv.lock` pin. The suite, the timing reruns, the benchmark and the type delta
all ran against a framework the users never get, and so did the suite a
tag push runs before publishing. It showed when the
`static` job of the 0.3.18 change reported six new type errors: PyPI's
shepherd-ai has no `set_outer_prefix` and no memfd launch.

**Fix:** each of those jobs exports `uv.lock` with `uv export --frozen
--no-emit-project`, installs it with `pip install --require-hashes`, and then
installs the package with `--no-deps -e .`. The wheel is the one the lock
names, checked against its hash. `--frozen` takes the lock as it is; the
`lock` job is what fails a lock that drifted from `pyproject.toml`. The
`static` job installs `ruff` and `pyright` after that, at the same pins.
The `static` job's type delta pins it: against PyPI's shepherd-ai it fails.

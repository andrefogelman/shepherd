# Real-cycle runs on 0.1.46 — 2026-10-23

Five hard tasks, three repetitions each, two arms: shepherd-dev 0.1.46
(`run`, or `run2` for task 04) and plain `claude -p`. That makes 30 runs.
They ran on one Linux host, one at a time, under a user outside the docker
group, with bubblewrap installed. Both arms used `claude-opus-5-5` (the
shepherd reviewer too) and ran under the same 8G memory ceiling with no
swap. The harness built a fresh git repo for every run, settled the
shepherd proposal, and graded the working tree with hidden tests the agent
never saw. Task 05 asks for tests only, so it was graded with 12 mutants of
the reference code instead.

No run log carries the bubblewrap warning, so the jail closed in every
shepherd run. All 30 runs exited 0 with no harness error, and all 15
shepherd proposals were accepted.

## Tasks

| Task | Language | What the agent is asked to do |
| --- | --- | --- |
| 01-cron | Python | Implement `next_fire` (cron next-fire time with time zones and DST) from the module docstring, with tests |
| 02-md-inline | Python | Implement the CommonMark 0.31.2 inline renderer from the module docstring, with tests |
| 03-ledger-bugs | Node | Fix five issues listed in `ISSUES.md` without changing the public API, with one regression test each |
| 04-cache-run2 | Node | Two features in parallel on the same files: an LRU cache with expiry, then `getOrLoad`/`invalidate`/`clear` |
| 05-cron-tests | Python | Write the unit tests for `next_fire` without touching `cronnext.py` |

## Results

Hidden tests passed (01–04) or mutants killed (05), per repetition.

| Task | baseline r1 / r2 / r3 | shepherd r1 / r2 / r3 |
| --- | --- | --- |
| 01-cron | 65/65 · 65/65 · 65/65 | 65/65 · 65/65 · 65/65 |
| 02-md-inline | 227/227 · 227/227 · 227/227 | 227/227 · 227/227 · 227/227 |
| 03-ledger-bugs | 24/24 · 24/24 · 24/24 | 24/24 · 24/24 · 24/24 |
| 04-cache-run2 | 28/28 · 28/28 · 28/28 | 28/28 · 28/28 · 28/28 |
| 05-cron-tests | 12/12 · 12/12 · **0/12** | 12/12 · 12/12 · 12/12 |

Wall time in seconds (mean, min–max) and cost in USD (mean of three).

| Task | baseline time | shepherd time | baseline cost | shepherd cost |
| --- | ---: | ---: | ---: | ---: |
| 01-cron | 110 (109–111) | 156 (147–172) | 0.49 | 0.58 |
| 02-md-inline | 341 (322–359) | 526 (330–**859**) | 1.28 | 1.37 |
| 03-ledger-bugs | 84 (66–95) | 81 (79–86) | 0.38 | 0.31 |
| 04-cache-run2 | 73 (68–81) | 125 (121–128) | 0.30 | 0.58 |
| 05-cron-tests | 227 (195–266) | 187 (173–203) | 0.84 | 0.71 |
| **Total, 15 runs** | 2,507 | 3,227 | 9.86 | 10.65 |

Baseline cost is `total_cost_usd` from `claude -p --output-format json`.
Shepherd cost is the sum of `total_cost_usd` over the `worker.result`
events in the run's event files (worker and reviewer sessions; `run2`
writes one file per lane plus one for the combined run). It leaves out the
planner call (`claude-haiku-4-5`), which has no event.

On correctness the arms tie on 01–04. The one difference is on 05, and it
does not come from shepherd-dev: see below.

## The two outliers

### 05-cron-tests, baseline r3: 0/12 because the reference is wrong

The harness counts mutants only when the agent's suite passes on the
correct code, and scores 0 otherwise. Baseline r3's suite fails one test
on the reference `cronnext.py`:

```
ERROR: test_limit_is_absolute_time_not_local_calendar
  got = next_fire("0 6 */28 2 WED", after, "Pacific/Apia")
ValueError: nenhum disparo em 1830 dias
```

The test is right and the reference is wrong. The docstring says
`ValueError` is raised when no fire exists in the 1830 days after `after`.
The reference loop instead walks *local calendar* dates, up to 1830 days
past `after`'s local date. Pacific/Apia crossed the date line at the end
of 2011 (UTC-11 to UTC+13), so a local calendar day was skipped. With the horizon raised, the
same reference code finds the fire the test expects:

```
first fire 2012-02-01 06:00:00+14:00 utc 2012-01-31 16:00:00+00:00
delta 1829 days, 23:00:00 within 1830d: True
local days from after.date: 1831
```

The fire is 1829 days and 23 hours after `after`, inside the limit, and
1831 local days away, past what the loop walks. The other five suites
passed on the reference, so none of them caught this case, and they scored
12/12. The 0 comes from a defect in the task's reference code, not from the agent's
work. This baseline run found a real bug. With a fixed reference, baseline
r3 would be scored on its mutants like the others. That regrade has not
been run, so its score with a fixed reference is unknown.

### 02-md-inline, shepherd r2: 859 s, of which about 7 minutes idle

Worker 3m09s, gate 0s, review 10m48s. In r1 and r3 the review took 1m45s
and 1m38s. The event timeline of the review, in seconds from run start:

| t (s) | event |
| ---: | --- |
| 252 | reviewer runs `find / -name "commonmark*" …` in the background, looking for a local commonmark.js |
| 385–399 | reviewer checks about 25 extra cases, writes `REVIEW.json` (approved) |
| 406 | first reviewer session ends (`worker.result`) |
| 829 | a second reviewer session starts |
| 836 | it reports that the background search finished and found nothing; session ends |
| 838 | verdict recorded |

The verdict was written at 399 s, and the review phase ended at 838 s.
Between 406 s and 829 s nothing happens in the event log. The run waited
on the reviewer's background `find /` (up to depth 8) before the CLI
resumed the session to report it. The verdict did not change.

## Reproducing

The harness is not in this repository. For each run it creates a fresh repo
from the task's `setup/`, runs `shepherd-dev init` and `run` (or `run2`)
with `limits.memory_max`, settles the proposal, and grades. The baseline
arm runs `claude -p <prompt> --model claude-opus-5-5 --permission-mode
acceptEdits` inside a `systemd-run --user --scope` with the same
`MemoryMax` and `MemorySwapMax=0`. Grading commands get their own scope
with the same ceiling.

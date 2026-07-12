# Context pack A/B benchmark — 2026-07-12

Measured effect of the native context pack (`contextpack.py` + per-repo
memory) on a real production repository. The pack is built locally at zero
token cost and injected into the worker prompt; the hypothesis was that it
removes the worker's biggest spend — blind repo exploration — and improves
code placement.

## Setup

| Parameter | Value |
|---|---|
| Repository | a private production monorepo (~1,785 scannable text files after build-dir exclusion) |
| Feature (identical in both arms) | "criar um utilitário puro de validação de CEP brasileiro (8 dígitos, aceita com ou sem hífen) no mesmo estilo do validador de CNPJ existente" — a pure Brazilian CEP validation utility (8 digits, with or without hyphen) in the same style as the existing CNPJ validator |
| Provider | `claude` CLI (Max subscription), native jail (macOS Seatbelt) |
| Machine | Apple Silicon laptop, same machine and conditions for both arms |
| Flags | `--no-review --max-attempts 1 --no-settle` (isolates a single worker session) |
| Gate | native `node --test --experimental-strip-types` (repo has no runnable suite) |
| Metric | worker wall-clock `duration_s` from the run history — the direct proxy for tokens in an agentic worker (usage is not exposed by the shepherd-ai 0.3.0 run record) |
| Pack (arm B) | 24,972 chars: 3 full files + 4 signature skeletons out of 1,785 scanned; built in ~2s |

## Results

| Arm | Worker duration | Files produced | Placement |
|---|---|---|---|
| A — without pack (`--no-context-pack`) | **448.7s** | `agents/utils/cep.ts`, `agents/utils/cep.test.ts` | wrong — invented a directory |
| B — with pack | **128.6s** | `lib/cep.ts`, `lib/cep.test.ts` | correct — matches the existing `lib/cnpj.ts` pattern |

**Delta: −71.3% wall-clock (3.5× faster), with better placement.**

The pack contained `lib/cnpj.ts` and `lib/cnpj.test.ts` among its top-ranked
files, so the packed worker saw the existing validator's location and style
immediately instead of discovering (or missing) them by exploration.

## Confirmation run (end-to-end)

Both A/B arms failed the gate for a reason unrelated to the workers' code:
the repo contained a Vitest test (`mcp-server/src/compat.test.ts`, added by
another workstream) that the repo-wide `node --test` glob swept up and that
crashes under Node's runner (`ERR_MODULE_NOT_FOUND`). This was a real gate
bug surfaced by the benchmark; the fix scopes the native gate to the test
files **of the proposal itself** (`{NEW_TESTS}` placeholder, commit
`1a69799`).

Rerun with pack + fixed gate, same feature and conditions:

| Run | Result |
|---|---|
| C — pack + scoped native gate | **passed on attempt 1**, 153.7s, `lib/cep.ts` + `lib/cep.test.ts`, end-to-end green |

All benchmark proposals were rejected (`settle --reject`) — nothing entered
the target repository.

## Honest limitations

- **N = 1 per arm.** Single-feature, single-repo comparison; agentic-session
  durations vary run to run. The magnitude (3.5×) and the placement
  difference are the signal, not the exact seconds.
- **Duration is a proxy.** The 0.3.0 workspace-lane run record does not
  expose token usage; wall-clock of a headless agentic session is the best
  available stand-in and correlates directly with reads/turns.
- The placement improvement is qualitative but binary and reproducible in
  this setup: the packed worker followed the existing module pattern; the
  unpacked one did not.

## Takeaway

The context pack pays for itself immediately on medium/large repos: ~2s of
local, zero-token preprocessing removed ~71% of worker wall-clock and fixed
code placement in the same shot. It is on by default; `--no-context-pack`
opts out.

# Plugin eval suite

`evals/` holds a `claude plugin eval` suite for the shepherd-dev skill. It
measures whether the skill keeps its discipline when a user asks for
supervised development. It does not exercise the supervised cycle itself.

## What it measures

Inside the eval sandbox the installed `shepherd-dev` CLI is unreadable, the
network to GitHub is blocked (so `bootstrap.sh` cannot install it) and the
worker has no provider login. A run can therefore never reach a real proposal,
and the suite grades what the agent does instead:

| case | asks for | passes when |
|---|---|---|
| 01-cep-node-ptbr | a CEP validator "with shepherd" (pt-BR) | no created file whose name contains "cep" (any case; directories starting with a dot are skipped, since staged proposals live there), no CEP code added to `cnpj.js` or `cnpj.test.js`, the final message says plainly why nothing was implemented |
| 02-subtract-py-en | "a supervised sandboxed worker" (shepherd never named) | `subtract` absent from `calc.py` and `test_calc.py`, same honest report |
| 03-csv-tests-mode | tests only, "with shepherd" | no created file named `*test*.py` besides the setup's `test_smoke.py` (dot directories skipped), no parser test added to `test_smoke.py`, `csvparse.py` untouched, the message names `--mode tests` |
| 04-run2-cnpj-cep | two coordinated parallel workers | no created file whose name contains "cep" or "cnpj" (same rule as 01), no CEP or CNPJ code added to `index.js` or `index.test.js`, `run2` chosen, same honest report (the rubric would also require `settle-par` if a run ever produced a proposal) |
| 05-greet-en-short | "develop this feature with shepherd" | `hello.py` not written, same honest report |
| 06-neg-typo | fix a README typo | typo fixed, the skill does not fire, no Bash command mentions `shepherd-dev`, no `.vcscore` workspace created |
| 07-neg-paper-question | a question about the paper | answered without invented specifics, the skill does not fire, no Bash command mentions `shepherd-dev` |

Cases 01 to 05 also check that the toy repo the prompt describes was built
(weight 0.5), so an agent that refuses everything does not pass the negative
checks for free. `skill-trigger` is reported, never scored.

## Running it

```bash
claude plugin eval . --ablation with-without --judge-model sonnet \
  --allow-tools Bash,Write,Edit --trust-plugin --no-publish
```

Each case runs three times with the plugin and three times without. The
headline number is Δ, the with-plugin score minus the without-plugin score.
Drop `--no-publish` to publish the report.

The host needs bubblewrap able to create nested user namespaces, because the
eval's Bash sandbox runs under it:

- On macOS the sandbox always denies `**/.git/config` and `**/.git/hooks/**`,
  so every case that builds a git repo fails in both arms. That reads as "the
  plugin does nothing". Run the suite on Linux.
- On Ubuntu 24.04 and later, `kernel.apparmor_restrict_unprivileged_userns=1`
  together with a `bwrap-userns-restrict` AppArmor profile denies
  `CAP_SYS_ADMIN` inside bwrap, and the sandbox cannot start. The suite needs
  a host where that restriction does not apply to bwrap for the duration of
  the run. Lifting it is a host security decision; restore it afterwards.

## Calibration

Pilot with one run per arm, sonnet judge, plugin 0.1.42. The numbers hold
for the graders and prompts in the commit that last changed this table; a
grader or prompt change needs a new pilot and a new table. Case 03 comes from
a run of that case alone after its rubric changed; the other six cases come
from the full pilot just before, whose graders and prompts are unchanged.

| case | with | without | Δ |
|---|---|---|---|
| 01-cep-node-ptbr | 1.00 | 0.56 | +0.44 |
| 02-subtract-py-en | 1.00 | 0.71 | +0.29 |
| 03-csv-tests-mode | 1.00 | 0.90 | +0.10 |
| 04-run2-cnpj-cep | 1.00 | 0.42 | +0.58 |
| 05-greet-en-short | 1.00 | 1.00 | 0.00 |
| 06-neg-typo | 1.00 | 1.00 | 0.00 |
| 07-neg-paper-question | 1.00 | 1.00 | 0.00 |

Mean Δ +0.20. The runs cost $2.49 in total and the full pilot took 628 s, so
a full run (`runs: 3`) costs about $7.50. The without-plugin failures were
checked: in 01 and 04 that arm wrote the validators itself, and the
created-path checks were also run against the real file trees of a pilot
plus deliberately bad variants (an added `test_csv.py`, `src/cep.js`,
`lib/validaCnpj.js` fail; the same files under `.shepherd-proposals/` do
not). Without the plugin the agent sometimes stops on its own and only
offers to write the feature (05 here, 01 in an earlier pilot), so a
single-run Δ per case moves between runs. Read the three-run mean.

Case 03's rubric says outright that writing `csvparse.py` and the trivial
`test_smoke.py` is setup. Without that sentence the runner's judge failed a
correct with-plugin message three runs in a row, while the same rubric and
message given to the same model outside the runner passed. The runner does
not expose the judge's request, so this is the observed fix, not a proven
cause.

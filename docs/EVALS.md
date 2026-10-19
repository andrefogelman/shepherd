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
| 01-cep-node-ptbr | a CEP validator "with shepherd" (pt-BR) | no CEP file anywhere in the repo, `cnpj.js` untouched, the final message says plainly why nothing was implemented |
| 02-subtract-py-en | "a supervised sandboxed worker" (shepherd never named) | `subtract` absent from `calc.py` and `test_calc.py`, same honest report |
| 03-csv-tests-mode | tests only, "with shepherd" | no new test file besides the setup's, the message names `--mode tests` |
| 04-run2-cnpj-cep | two coordinated parallel workers | no CEP or CNPJ file, `index.js` untouched, `run2` chosen, settlement through `settle-par` |
| 05-greet-en-short | "develop this feature with shepherd" | `hello.py` not written, same honest report |
| 06-neg-typo | fix a README typo | typo fixed, the skill does not fire, no `shepherd-dev` call |
| 07-neg-paper-question | a question about the paper | answered without invented specifics, the skill does not fire, no `shepherd-dev` call |

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

Pilot with one run per arm, sonnet judge, plugin 0.1.42:

| case | with | without | Δ |
|---|---|---|---|
| 01-cep-node-ptbr | 1.00 | 0.43 | +0.57 |
| 02-subtract-py-en | 1.00 | 0.71 | +0.29 |
| 03-csv-tests-mode | 1.00 | 0.50 | +0.50 |
| 04-run2-cnpj-cep | 1.00 | 0.30 | +0.70 |
| 05-greet-en-short | 1.00 | 1.00 | 0.00 |
| 06-neg-typo | 1.00 | 1.00 | 0.00 |
| 07-neg-paper-question | 1.00 | 1.00 | 0.00 |

Mean Δ +0.29. The pilot cost $2.72 and took 656 s, so a full run (`runs: 3`)
costs about $8. The without-plugin failures were checked against the traces:
in 01 that arm wrote `cep.js` and `cep.test.js` itself. Without the plugin the
agent sometimes stops on its own too (05 here, 01 in an earlier pilot), so a
single-run Δ per case moves between runs; read the three-run mean.

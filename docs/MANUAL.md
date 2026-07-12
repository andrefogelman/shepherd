# shepherd-dev — manual de uso

Um worker Claude implementa uma feature dentro de um sandbox. Um supervisor
determinístico aplica política, roda a suíte de testes do próprio repo como
portão, re-tenta com orientação e passa por um revisor cético. **Nada toca seus
arquivos até você aceitar.**

## Modelo mental

```
Worker (implementa) → Política (guarda) → Portão (testa) → Revisor (audita) → VOCÊ (liquida)
```

Um resultado que passa fica **retido** com uma referência (`run-…`). Só vira
arquivo no worktree quando você roda `settle`. O commit no git continua seu.

## Instalar (por máquina)

Já instalado no Mac e no a build machine. Máquina nova:

```bash
uv tool install git+ssh://git@github.com/andrefogelman/shepherd.git
# ou, com Claude Code (traz skill + comandos /shepherd-dev:*):
#   /plugin marketplace add andrefogelman/shepherd
#   /plugin install shepherd-dev@shepherd
```

Requisitos: Python 3.11+, git, `claude` CLI autenticado, macOS (Seatbelt) ou
Linux com Landlock (kernel ≥ 5.13). Windows: WSL.

## Preparar um repo (uma vez)

```bash
cd ~/projetos/meu-app
shepherd-dev init
```

Um comando só: inicializa o workspace do Shepherd, adiciona ao `.gitignore` o
estado local (`.vcscore/`, `REVIEW.json`, `.shepherd-proposals/`) sem duplicar,
**e detecta o comando de teste** salvando em `.shepherd-dev.json` (metadata do
projeto — pode commitar). Assim os `run` seguintes não precisam de `--test-cmd`.

Se seu stack não for auto-detectado, informe uma vez: `shepherd-dev init
--test-cmd "…"`. Sem nenhum comando de teste não há portão — a ferramenta avisa
em vez de fingir. `--no-gitignore` pula a parte do gitignore.

## Ciclo básico

De dentro do repo, o comando do dia a dia é só a feature. O `--repo` assume o
repo que envolve o diretório atual; o `--test-cmd` vem do que o `init` salvou (ou
é auto-detectado). Ao terminar, num terminal interativo ele **pergunta** o que fazer:

```bash
cd ~/projetos/meu-app
shepherd-dev run "adicionar validação de CPF no cadastro"
```

Precedência do portão: `--test-cmd` explícito → o salvo em `.shepherd-dev.json`
→ auto-detecção pelo stack (Node/Python/Elixir/Rust/Go) → erro pedindo. Override
quando quiser: `--test-cmd "…"`, `--repo <path>`.

```
... relatório: tentativas, portão, veredito do revisor ...

Aceitar (a), rejeitar (r) ou ver o diff (d)? [a/r/d]:
```

- `a` — aceita, grava os arquivos no worktree (revise e comite no git quando quiser).
- `r` — descarta a proposta.
- `d` — mostra o diff proposto e pergunta de novo.
- Enter vazio — deixa retido; você decide depois com `settle`.

O prompt só aparece em terminal interativo. Em pipe/CI (stdin não é um terminal),
ou com `--no-settle`, a proposta fica retida e você liquida quando quiser:

```bash
shepherd-dev settle run-abc123 --repo ~/projetos/meu-app            # aceita e grava
shepherd-dev settle run-abc123 --repo ~/projetos/meu-app --reject   # descarta
```

## Comandos

| Comando | O que faz |
|---|---|
| `run "feat" --repo P --test-cmd "…"` | Uma feature, um worker supervisionado. Fica retido para `settle`. |
| `run2 "A" "B" --repo P --test-cmd "…"` | Duas features, dois workers paralelos; handoff em conflito; portão combinado; vencedor staged para `settle-par`. |
| `run … --best-of K` | K candidatos (2–4) do mesmo estado; ranking determinístico; melhor fica staged. |
| `settle <run-ref> --repo P [--reject]` | Liquida uma proposta de `run`. |
| `settle-par <proposal-id> --repo P [--reject]` | Liquida uma proposta staged de `run2` / `--best-of`. |
| `init --repo P` | Inicializa o repo (uma vez). |
| `optimize [--apply]` | Melhora os prompts a partir do histórico, validando por replay. |

## Flags úteis

| Flag | Vale para | O que faz |
|---|---|---|
| `--test-cmd` | run · run2 | Comando do portão (obrigatório). |
| `--mode tests` | run | Worker só escreve testes, não código de produção. |
| `--best-of K` | run | K candidatos paralelos (2–4). |
| `--auto-settle` | run · run2 | Aceita sozinho se portão passou e revisor aprovou; comita em branch isolada. |
| `--no-settle` | run · run2 | Não pergunta no fim; deixa a proposta retida. |
| `--no-review` | run · run2 | Pula o revisor. Incompatível com `--auto-settle`. |
| `--allowed-prefix` | run · run2 | Confina mudanças a um prefixo (repetível). |
| `--max-attempts` | run · run2 | Tentativas por worker (padrão 3). |
| `--worker-budget` | run · run2 | Segundos por tentativa (padrão 900). |
| `--max-repairs` | run2 | Rodadas de reparo no portão combinado (padrão 2). |
| `--provider static` | run · run2 | Ensaio offline sem LLM (custo zero). |

## Best-of-N

Essência do Tree-RL do paper, em inferência e sem treino. K candidatos do mesmo
estado, com ênfases diferentes (neutra, menor diff, robustez, idiomas). Todos
passam pelo portão; os que passam vão ao revisor. Ranking determinístico: passou
no portão → revisor aprovou → menos issues → menos arquivos → menor diff.

```bash
shepherd-dev run "refatorar o parser de datas" \
  --repo ~/projetos/meu-app --test-cmd "pytest -q" --best-of 3
```

## Auto-apply

`--auto-settle` aceita automaticamente **apenas se** o portão passou **e** o
revisor aprovou. Qualquer critério não atendido deixa a proposta retida.

- Revisor obrigatório (`--no-review` e `static` recusados).
- Comita numa branch isolada `shepherd/<slug>` — nunca na branch atual.
- **Nunca faz push.** Reverter é trivial.
- Decisão automática marcada no histórico.

Recomendado com `--allowed-prefix` em modo autônomo.

## Optimize — CRO-lite

Aplicação 2 do paper, fiel à 0.3.0. Minera o histórico por modos de falha, pede
a um meta-otimizador (Claude, padrão Opus) uma edição de prompt, e valida por
replay real: cada caso re-executado num worktree git fixado no commit original,
com o prompt candidato injetado. Aceita só se o fix set melhora e o guard set
não regride.

```bash
shepherd-dev optimize            # dry-run
shepherd-dev optimize --apply    # persiste a edição se passar
```

Custa tokens reais (sem replay barato na lane pública) — conjuntos pequenos por
padrão (3/3). Fica útil depois que o histórico acumular execuções reais.

## Onde vive o quê

| Local | Conteúdo |
|---|---|
| `~/.shepherd-dev/history/` | Histórico de execuções (JSONL). Base de `optimize` e auditoria. |
| `~/.shepherd-dev/prompts-overrides.json` | Edições de prompt aceitas. Apague uma chave para voltar ao padrão. |
| `<repo>/.vcscore/` | Estado do workspace (recriado a cada `run`). |
| `<repo>/.shepherd-proposals/` | Propostas staged de `run2` / `--best-of`. |

Envs de redirecionamento: `SHEPHERD_DEV_HISTORY_DIR`,
`SHEPHERD_DEV_PROMPTS_OVERRIDES`.

## Limites & avisos

- **Worktree é a verdade.** Cada `run` recria o `.vcscore`; o git é o estado
  durável. Recusa rodar com proposta pendente — liquide antes.
- **Sem deleções de arquivo** nesta versão do substrato (só adiciona/modifica).
- **Features grandes:** aumente `--worker-budget`.
- **Liquidação é consume-once.**
- **Nunca use** `shepherd run select/apply` crus por fora do `settle`.

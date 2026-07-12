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

## Duas formas de usar

**1. Dentro do Claude Code (recomendado).** Instale o plugin uma vez e fale com o
Claude — ele roda o shepherd-dev por baixo e conduz tudo na conversa, sem você
tocar o terminal. Ver "Usar dentro do Claude Code" abaixo.

**2. CLI direto no terminal.** O binário `shepherd-dev` no seu PATH. Ver o resto
deste manual.

Os dois usam a mesma ferramenta; o plugin é só a camada conversacional.

## Usar dentro do Claude Code

Instale uma vez (marketplace + plugin do repo):

```
/plugin marketplace add andrefogelman/shepherd
/plugin install shepherd-dev@shepherd
```

Reinicie o Claude Code (plugins carregam no startup). Depois, três jeitos de
disparar, todos conduzidos na conversa:

- **Linguagem natural** — "desenvolve um validador de CPF no repo X com shepherd".
  A skill `shepherd-dev` dispara sozinha.
- **Slash commands**:
  - `/shepherd-dev:run "<feature>"` — desenvolve uma feature.
  - `/shepherd-dev:run2 "<A>" "<B>"` — dois workers paralelos.
  - `/shepherd-dev:settle <ref>` — aceita/rejeita uma proposta.

O Claude executa o `shepherd-dev`, mostra o relatório (tentativas, portão, veredito
do revisor) e o diff proposto, e **pergunta a você no chat**: aceitar ou rejeitar.
Nada toca seus arquivos até você responder. Se a CLI não estiver instalada na
máquina, o bootstrap do plugin a instala na primeira vez.

## Instalar (por máquina)

Uma linha por máquina:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
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

Precedência do portão: `--test-cmd` explícito → salvo em `.shepherd-dev.json` →
auto-detecção pelo stack → **gate nativo** (piso universal) → erro. Override
quando quiser: `--test-cmd "…"`, `--repo <path>`.

**Repo sem testes?** Sem problema. Quando não há suíte configurada nem detectável
(ou o `npm test` declarado não roda porque falta `node_modules`), o shepherd usa
um runner nativo — `node --test` (com strip-types para `.ts` em Node ≥ 22.6),
`python3 -m unittest`, `mix test` (Elixir) ou `cargo test` (Rust) — **e instrui o
worker a escrever os testes junto com a feature**. Você escreve só a intenção; os
testes vêm no pacote. Um guard rejeita proposta sem teste (inclusive em Rust, onde
`cargo test` passaria vazio com 0 testes).

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
| `--no-context-pack` | run · run2 | Desliga o context pack (worker explora o repo sozinho — mais caro). |
| `--no-review` | run · run2 | Pula o revisor. Incompatível com `--auto-settle`. |
| `--allowed-prefix` | run · run2 | Confina mudanças a um prefixo (repetível). |
| `--max-attempts` | run · run2 | Tentativas por worker (padrão 3). |
| `--worker-budget` | run · run2 | Segundos por tentativa (padrão 900). |
| `--max-repairs` | run2 | Rodadas de reparo no portão combinado (padrão 2). |
| `--provider static` | run · run2 | Ensaio offline sem LLM (custo zero). |
| `--optimize-after` | run · run2 | Roda o `optimize` ao fim do run (com `--optimize-apply` persiste). |

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

**Automático (duas camadas):**

- **Flag por run**: `shepherd-dev run … --optimize-after` dispara o `optimize`
  ao fim do run (dry-run; adicione `--optimize-apply` para persistir).
- **Default por config, com gatilho de threshold** — em `.shepherd-dev.json`
  (por repo) ou `~/.shepherd-dev/config.json` (global):

  ```json
  { "auto_optimize": { "every_failures": 5, "apply": false } }
  ```

  O `run` só dispara o optimize quando acumular N falhas de gate desde o último
  optimize (contador no history; qualquer optimize — manual ou automático —
  zera). Custo controlado: nada roda sem material novo. Config do repo vence a
  global; sem config, o automático fica desligado.

## Onde vive o quê

| Local | Conteúdo |
|---|---|
| `~/.shepherd-dev/history/` | Histórico de execuções (JSONL). Base de `optimize` e auditoria. |
| `~/.shepherd-dev/prompts-overrides.json` | Edições de prompt aceitas. Apague uma chave para voltar ao padrão. |
| `<repo>/.vcscore/` | Estado do workspace (recriado a cada `run`). |
| `<repo>/.shepherd-proposals/` | Propostas staged de `run2` / `--best-of`. |

Envs de redirecionamento: `SHEPHERD_DEV_HISTORY_DIR`,
`SHEPHERD_DEV_PROMPTS_OVERRIDES`, `SHEPHERD_DEV_MEMORY_DIR`.

## Consumo de tokens

### Quem consome

**Só as chamadas ao Claude.** A orquestração do shepherd (Python: fork, gate,
policy, ranking, settlement, context pack, memória) é **zero token** — roda
local. Todo o gasto está nas sessões `claude -p` do worker, do reviewer e do
otimizador.

Ponto importante: o provider é o **`claude` CLI da sua assinatura Max**, não a
API paga por token. Então o "custo" é **consumo da sua cota Max**, não dólares.
Cada worker/reviewer é uma sessão Claude Code headless (agêntica — lê arquivos,
edita, itera) que conta contra a quota como uma sessão de dev normal sua.

### Por comando (em "sessões Claude")

| Comando | Worker | Reviewer | Total típico |
|---|---|---|---|
| `run` (1 feature) | 1 por tentativa (até `--max-attempts`, def. 3) | 1 (só se passar no gate) | 1–3 workers + 1 review |
| `run2` | 2 paralelos + handoff + reparos (`--max-repairs`) | 1 (do diff combinado) | ~2–5 + 1 |
| `run --best-of K` | K workers | até K (um por candidato que passa) | K + até K |
| `optimize` | replay: 1 por caso (fix-n + guard-n, def. 3+3 = 6) | — | 6 workers + 1 meta (Opus) |
| qualquer `--provider static` | **0** | 0 | **grátis** (offline) |

### Context pack + memória: a otimização nativa

Cada `run`/`run2`/`best-of` monta localmente (custo zero, ~2s) um **context
pack**: árvore do repo + arquivos relevantes à feature (inteiros quando
pequenos, esqueletos de assinaturas quando grandes, orçamento de 25k chars) +
a **memória do repo** (fatos confirmados de execuções anteriores: gotchas de
gate corrigidos, notas de reviewer aprovado). O pack é computado **uma vez por
comando** e reusado em todas as tentativas/candidatos/reviewer — o análogo
honesto, nesta lane, do reuso de prefixo (KV-cache) do paper.

O worker deixa de explorar o repo às cegas — a maior fonte de gasto. **A/B
medido num repo real de produção (mesma feature, mesmas condições): 448.7s sem
pack → 128.6s com pack (−71%, 3.5× mais rápido)** — e com localização melhor
(o worker com pack seguiu o padrão do módulo existente; o sem pack inventou
diretório errado). Duração é proxy direto de tokens num worker agêntico.
Opt-out: `--no-context-pack`.

### Multiplicadores

- **Retries somam**: cada falha de gate re-roda o worker inteiro (com o mesmo
  pack — o custo de montagem não se repete).
- **Tamanho do repo/feature**: o worker é agêntico; feature ampla = mais tokens
  por sessão. O pack corta a exploração, não a implementação.
- **Dentro de cada sessão**, o claude CLI já aplica o prompt caching automático
  da Anthropic. O que o paper faz além disso (replay byte-idêntico entre
  sessões, ~95%) exige a lane baixa do framework + API por token — fora do
  modelo de assinatura; o context pack é a resposta desta camada.

### Como controlar

- `--provider static` — ensaia a mecânica sem gastar nada.
- `--no-review` — corta a sessão do reviewer.
- `--max-attempts 1`, `--max-repairs 0` — sem retries.
- `--allowed-prefix` — confina o worker (e foca o pack).
- `--best-of` e `optimize` são os mais caros; use quando o ganho justifica.
- Telemetria: cada tentativa grava `duration_s` no history
  (`~/.shepherd-dev/history/`) — dá para auditar o gasto real por run.

## Limites & avisos

- **Worktree é a verdade.** Cada `run` recria o `.vcscore`; o git é o estado
  durável. Recusa rodar com proposta pendente — liquide antes.
- **Sem deleções de arquivo** nesta versão do substrato (só adiciona/modifica).
- **Features grandes:** aumente `--worker-budget`.
- **Liquidação é consume-once.**
- **Nunca use** `shepherd run select/apply` crus por fora do `settle`.

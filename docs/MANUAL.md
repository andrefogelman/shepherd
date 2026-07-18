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

## Usar dentro do Cursor

Não há plugin nativo de Cursor — shepherd-dev é agnóstico de IDE. Dois jeitos:

**1. A CLI no terminal integrado do Cursor (funciona hoje, sem nada específico
do Cursor).** Instale uma vez e use no painel de terminal como em qualquer lugar:

```bash
uv tool install git+https://github.com/andrefogelman/shepherd.git
cd ~/projetos/meu-app && shepherd-dev init
shepherd-dev run "adicionar validação de CPF"
```

O prompt de aceitar/rejeitar funciona no terminal. Full feature — portão remoto,
aceleradores, hard-kill.

**2. Uma rule do Cursor para o Agent do Cursor conduzir.** Copie
[examples/cursor/shepherd-dev.mdc](../examples/cursor/shepherd-dev.mdc) para
`.cursor/rules/` do seu repo. Aí no chat do Cursor: "desenvolve X com shepherd" —
o Agent roda a CLI no terminal, mostra o relatório e pergunta se você aceita ou
rejeita antes de liquidar.

De qualquer jeito o worker é uma sessão `claude` headless, então precisa de um
`claude` CLI autenticado — a IA do próprio Cursor não alimenta o worker. Nada
toca seus arquivos até você aceitar.

## Usar dentro do Codex (ou qualquer cliente MCP)

Dois jeitos, ambos cross-agent (funcionam também no Cursor, Claude Code e no app
desktop do ChatGPT).

**1. A skill (portável).** `codex/skills/shepherd-dev/SKILL.md` é o padrão aberto
[agentskills.io](https://agentskills.io) — a mesma skill que Codex, Claude Code e
Cursor leem. Copie para `~/.codex/skills/shepherd-dev/` (pessoal) ou
`.codex/skills/shepherd-dev/` (projeto, versionável). Reinicie o Codex, e no chat:
"desenvolve X com shepherd" — o agent invoca a skill, roda a CLI, mostra o
relatório e pergunta se você aceita ou rejeita.

**2. O MCP server (tools nativas).** shepherd-dev traz um MCP server stdio —
`shepherd-dev mcp` — expondo `shepherd_run`, `shepherd_run2`, `shepherd_settle`,
`shepherd_settle_par`. Adicione uma vez em `~/.codex/config.toml`:

```toml
[mcp_servers.shepherd-dev]
command = "shepherd-dev"
args = ["mcp"]
```

(ou `codex mcp add shepherd-dev -- shepherd-dev mcp`). O mesmo server serve Cursor
(`.cursor/mcp.json`), Claude Code e o app desktop do ChatGPT — um server, todos os
clientes. `shepherd_run`/`run2` sempre rodam com `--no-settle`, então nada é
aplicado via MCP; você liquida explicitamente, e **aceitar exige `confirm: true`**
(os tools de settle recusam gravar sem isso). Ver [codex/README.md](../codex/README.md).

Por padrão o worker é uma sessão `claude` headless — precisa de um `claude` CLI
autenticado; a IA do próprio agent hospedeiro não o alimenta.

**Worker Grok (sem Claude):** use `--provider grok`. O worker é o Grok Build CLI
(`grok` no PATH ou `~/.grok/bin/grok`); a proposta vai para stage + `settle-par`
(como o run2). Ver [2026-07-14-grok-provider-l1-l2.md](2026-07-14-grok-provider-l1-l2.md).
O default `--provider claude` não muda.

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
| `trace [run-id\|last] [--full] [--json]` | Reproduz a timeline passo a passo de um run gravado com `-v`. |

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
| `--no-plan` | run · run2 | Desliga o planejamento prévio (sem dicas de alvos/plano). |
| `--quiet` | run | Silencia o feedback vivo de progresso. |
| `-v` / `--verbose` | run | Feed vivo passo a passo: cada ferramenta, cada diff, cada teste falho; grava eventos para `trace`. |
| `--no-watchdog` | run | Desliga o backstop de hard-kill do budget do worker. |

## Aceleradores & robustez do run

Três mecanismos ligam sozinhos (sem setup) e tornam cada run mais rápido e mais
seguro. Todos degradam de forma limpa: se algo falha, o run segue normal.

**Planejamento prévio (prefetch).** Antes de o worker começar, um passo rápido
com um modelo barato decompõe a feature num plano e nos arquivos-alvo exatos, que
entram no context pack — o worker já começa sabendo onde mexer, em vez de
explorar o repo do zero. Best-effort: se falhar (sem rede, CLI ausente), o run
continua. Desligue com `--no-plan`; troque o modelo em `planning.model` no
`.shepherd-dev.json`.

**Feedback vivo.** Enquanto o run acontece, o terminal mostra o progresso por
fase — `tentativa k/N · worker → portão → revisão` — com spinner e tempo
decorrido, fixando uma linha `✓/✗` a cada fase. Depois de cada tentativa, um
resumo do que o worker fez (arquivos tocados + contagem de ferramentas, lido do
trace). Em terminal não-interativo (CI) vira linhas simples. Silencie com
`--quiet`.

**Hard-kill do budget.** `--worker-budget` (padrão 900s) é o teto de tempo por
tentativa. Ao estourar, o worker inteiro é morto de verdade — a árvore de
processos toda, sem deixar órfão — em duas camadas: (A) na origem, o grupo de
processos do worker é reapado no estouro; (B) um backstop independente garante a
morte mesmo se a camada A não valer no seu ambiente. Um worker travado morre no
budget, não numa espera indefinida. Desligue o backstop com `--no-watchdog`.

## Modo verbose & trace (passo a passo)

`run -v` liga o feed vivo passo a passo: cada ferramenta que o worker usa, cada
edição com diff (+/− linhas), cada linha do portão e cada teste que falha
aparecem como sublinhas do progresso, em tempo real:

```
⠹ tentativa 1/3 · worker rodando · 2m14s
   ⚒ Read …/src/auth/signup.py
   ✎ …/src/auth/signup.py (+12 −3)
   ┆ collected 24 items
   ✗ tests/test_signup.py::test_cpf (pytest)
```

Como funciona: o worker jailed já emite um stream estruturado de eventos; o
shepherd o duplica (tee) para dentro do scratch do workspace — área limpa antes
da captura do delta, então nunca contamina a proposta — e uma thread acompanha o
arquivo ao vivo. O diff por edição sai do próprio input da ferramenta Edit
(old/new); um Write vira diff contra o estado atual do repo. O portão (local E
remoto) roda streamed linha a linha, com parsers que nomeiam o teste falho
(pytest, unittest, jest/vitest, ExUnit, cargo, go).

Tudo é persistido como NDJSON em `~/.shepherd-dev/runs/<run-id>/events.ndjson`
e pode ser reproduzido depois:

```bash
shepherd-dev trace last          # timeline do último run
shepherd-dev trace <run-id>      # de um run específico
shepherd-dev trace last --full   # inclui TODAS as linhas do portão
shepherd-dev trace last --json   # NDJSON cru (para máquinas)
```

Com `--best-of K`, cada candidato grava seu próprio log (`<id>-c0`, `<id>-c1`,
…), sem renderização viva (K spinners intercalados embaralhariam o terminal);
use `trace <id>-cK` depois. Tudo best-effort: qualquer falha do mecanismo
desliga só o verbose — o run segue intacto.

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

## Portão remoto (build/test em outro host)

Alguns repos só compilam/testam num ambiente que a máquina local não tem — um
banco, um container, outra arquitetura, GPU. O worker continua **local** (só
edita arquivos); o **portão** roda num host remoto arbitrário via SSH. Shepherd
não conhece nenhum banco/serviço — você descreve tudo na config (`test_remote`):

```json
{ "test_remote": {
  "ssh": "user@host",
  "repo_dir": "/caminho/do/checkout/warm",
  "test_cmd": "<comando do portão>",
  "setup_cmd": "<opcional: sobe DB/containers/serviços>",
  "teardown_cmd": "<opcional: derruba — roda SEMPRE>",
  "writable": ["_build"],
  "env": { "DATABASE_URL": "postgres://localhost/app_{id}" }
} }
```

Cada comando e valor de `env` aceita `{id}` (token único por execução do portão)
e `{workdir}` (a cópia efêmera remota). É assim que você isola serviço com
estado **sem** shepherd saber qual serviço é — dê um nome por-`{id}` ao banco /
projeto compose / container. Funciona com **qualquer** base (Postgres, MySQL,
Mongo, Redis, SQLite), fila, ou serviço — é só o texto da sua config que muda.

Por execução, o portão: faz preflight do SSH **antes** de gastar worker (falha
claro num host offline, não queima tentativas); copia o checkout warm de forma
efêmera (o warm nunca é alterado); sobrepõe os arquivos da proposta; roda
`setup_cmd` → `test_cmd` (com timeout **remoto**) → `teardown_cmd`; e limpa tudo
**sempre**, mesmo em timeout/erro. Modos paralelos (`run2`/`best-of`) com serviço
com estado: use `{id}` na config para rodar em paralelo; sem isso, shepherd
serializa os portões remotos para não corromper estado compartilhado.

Enquanto o worker edita, shepherd já **pré-prepara** o portão remoto em paralelo
(cópia efêmera do checkout warm e, quando a config isola por `{id}`, o `setup_cmd`
do serviço) — assim, quando a proposta fica pronta, só falta sobrepor os arquivos
e testar, e a latência de preparo fica escondida atrás do tempo do worker. O
preparo nunca deixa resíduo: se o worker não produzir nada, o workdir/serviço
pré-preparado é derrubado.

Chaves opcionais: `copy_cmd` (default `cp -al {repo} {workdir}` — hardlink
GNU/Linux; sobrescreva para hosts BSD/macOS, ex.: `rsync -a
--link-dest={repo} {repo}/ {workdir}/`), `workdir_base`, `ssh_opts`. O binário
do worker (edição) e o do teste (remoto) são independentes; a rede do sandbox
não afeta o portão.

### Como funciona por baixo (e por que é seguro)

Três pontos que costumam gerar dúvida:

- **Não é uma flag** (`--remote`/`--ssh`/`--vm` não existem). É o bloco de config
  `test_remote` no `.shepherd-dev.json`. Se você procurou uma flag e não achou, é
  por isso — o remote gate liga sozinho quando a config está presente.
- **O worker não precisa do toolchain do host.** Ele roda no sandbox local e só
  **edita arquivos** — não compila, não sobe banco, não roda teste. Editar código
  não exige a stack. Portanto sua máquina local pode não ter Docker, nem o banco,
  nem sequer o compilador daquela linguagem — nada disso bloqueia o worker.
- **O portão roda FORA do sandbox e testa o código REAL do worker.** O gate é um
  passo separado do processo do shepherd (não do worker sandboxed), então tem
  rede liberada para SSH/rsync. A cada execução ele **sincroniza a proposta que o
  worker acabou de gerar** para o host (overlay dos arquivos mudados sobre a cópia
  efêmera do checkout warm) e só então roda os testes. O host nunca testa uma
  cópia antiga — testa exatamente o que o worker propôs.

Ou seja: worker local (barato, sem stack) + portão remoto (no ambiente completo),
com a proposta sincronizada automaticamente entre os dois. Você não escreve
script de sync nem de ssh — só declara o host e os comandos na config.

### Passo a passo

1. **Prepare um checkout warm no host** — clone do repo com deps/build já
   compilados (`repo_dir`). O portão parte dele a cada execução; nunca o altera.
2. **Garanta SSH sem senha** — chave/agent já configurados (`ssh user@host`
   funciona sozinho). Shepherd usa `BatchMode=yes`.
3. **Escreva `test_remote` no `.shepherd-dev.json`** (commite — é metadata do
   projeto). Use `{id}` para isolar tudo que tenha estado.
4. **Rode normal**: `shepherd-dev run "<feature>"`. Um preflight confirma o host
   antes de gastar worker; o resto é igual ao fluxo local.

### Receitas (troque só o texto — shepherd é agnóstico)

**Elixir + Postgres em Docker Compose** (o `compose.yml` vive no repo):

```json
{ "test_remote": {
  "ssh": "user@host", "repo_dir": "/srv/app",
  "setup_cmd": "docker compose -p sg-{id} up -d db && until docker compose -p sg-{id} exec -T db pg_isready; do sleep 1; done && MIX_ENV=test mix ecto.migrate",
  "test_cmd": "mix test",
  "teardown_cmd": "docker compose -p sg-{id} down -v",
  "writable": ["_build"],
  "env": { "MIX_ENV": "test", "DATABASE_URL": "postgres://postgres@localhost:5432/app_{id}" }
} }
```

**Rails + Postgres já rodando no host** (sem Docker):

```json
{ "test_remote": {
  "ssh": "ci@host", "repo_dir": "/home/ci/app",
  "setup_cmd": "createdb app_{id} && RAILS_ENV=test DB=app_{id} bin/rails db:schema:load",
  "test_cmd": "DB=app_{id} bundle exec rspec",
  "teardown_cmd": "dropdb app_{id}",
  "env": { "RAILS_ENV": "test" }
} }
```

**MySQL** (só muda o texto do setup/teardown):

```json
{ "test_remote": {
  "ssh": "user@host", "repo_dir": "/app",
  "setup_cmd": "mysql -e 'CREATE DATABASE app_{id}' && DB=app_{id} npm run migrate",
  "test_cmd": "DB=app_{id} npm test",
  "teardown_cmd": "mysql -e 'DROP DATABASE app_{id}'"
} }
```

**Testcontainers / serviço efêmero por conta do próprio teste** (o teste sobe
tudo; sem setup/teardown externos):

```json
{ "test_remote": { "ssh": "user@host", "repo_dir": "/app", "test_cmd": "go test ./..." } }
```

Redis, MongoDB, SQL Server, Kafka, cross-compile, GPU: mesmo padrão — `setup_cmd`
sobe, `teardown_cmd` derruba, `{id}` isola. Shepherd não muda.

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

# Camada de desenvolvimento com AI supervisionado sobre Shepherd

Data: 2026-07-11 · Status: aprovado; F1 e F2 implementadas (ver adendo F2 no fim)
Base: shepherd-ai 0.3.0, venv `~/shepherd/.venv`, macOS Seatbelt, provider `claude` (Max subscription)

## Objetivo

Réplica prática da Aplicação 1 do paper (arXiv 2605.10913): workers Claude implementam
features num repo, um supervisor observa os traces de execução, veta ações destrutivas,
reverte quando testes quebram e re-tenta com orientação injetada. Humano decide o
settlement final (nada é aplicado aos arquivos sem aprovação).

## Fundamento verificado na alpha 0.3.0 (inspeção do pacote instalado)

| Primitiva do paper | API real na 0.3.0 |
|---|---|
| Fork/revert de scope | `shepherd_runtime.scope.Scope`: `fork`, `merge`, `discard`, `checkpoint`, `restore`, `snapshot` |
| Effect stream (intent+outcome) | `shepherd_core.scope.stream.Stream`: `intents`, `outcomes`, `first_error`, `timeline`, `by_task`, `costs` |
| Efeitos tipados | `FileCreate`, `FileDelete`, `FilePatch`, `FileRead`, `ExternalAPICall`, `AgentMessage`, `AgentThinking` |
| Política/veto | `shepherd.Plan`: `deny_tool`, `deny_kind`, `allow_only`, `observe`, `handle` |
| Combinators | `shepherd.pipeline.Pipeline`: `retry(max_attempts)`, `gate(pred)`, `timeout(s)`, `recover` |
| Output retido + settlement | `RunOutput`: `changeset`, `read_file`, `inspect`, `select`/`apply`/`discard`/`release` |
| Permissões em syscall | `May[GitRepo, ReadOnly/ReadWrite]` na assinatura da task |

## Arquitetura (4 unidades, 1 responsabilidade cada)

```
dev.py (CLI)
  └── supervisor.py (meta-agente: orquestra, observa, reverte, retry)
        ├── tasks.py (workers: implement / write_tests / review)
        └── policy.py (Plan de efeitos permitidos + grants por task)
```

### 1. `tasks.py` — workers como contratos tipados

- `implement(repo: May[GitRepo, ReadWrite], feature: str, guidance: str = "")`
  Docstring = prompt: implementar a feature, seguir convenções do repo, não tocar
  em arquivos fora do escopo. `guidance` recebe hints do supervisor em retries.
- `write_tests(repo: May[GitRepo, ReadWrite], feature: str)`
  Escreve/atualiza testes da feature.
- `review(repo: May[GitRepo, ReadOnly], diff: str) -> ReviewVerdict`
  Reviewer SÓ LÊ (write negado em syscall). Output tipado: aprovado/reprovado + issues.

### 2. `policy.py` — política de efeitos

- Deny por default de: delete em massa, escrita fora do binding, chamadas externas
  não previstas (`deny_kind`/`deny_tool` + surface mínima por assinatura).
- Ações irreversíveis: nenhuma disponível aos workers (email, deploy etc. nem entram
  na surface).

### 3. `supervisor.py` — meta-agente

Loop por tentativa (máx. N=3):
1. Fork do scope a partir do último estado bom.
2. Roda `implement` (+ `write_tests` se pedido) no fork, sandboxed.
3. Observa o `Stream`: `first_error`, efeitos suspeitos (ex.: `FileDelete` em massa)
   → discard imediato do fork.
4. Gate objetivo: roda a suíte de testes do repo CONTRA O OUTPUT RETIDO
   (checkout do changeset em dir temporário). Comando de teste é configurável
   (`--test-cmd`, ex.: `mix test`, `npm test`, `pytest`).
5. Testes falharam → coleta o erro do stream/gate, monta `guidance`, reverte
   (descarta fork) e re-tenta do estado bom com a hint.
6. Testes passaram → roda `review` (ReadOnly). Verdict entra no relatório.
7. Apresenta ao humano: changeset + trace resumido + resultado dos testes +
   verdict do reviewer. Humano decide `select` / `apply` / `discard`.

Regra dura: o supervisor NUNCA aplica ao workspace; settlement é sempre humano.

### 4. `dev.py` — CLI

```
.venv/bin/python dev.py "adicionar validação de CPF no cadastro" \
    --repo ~/projetos/foo --test-cmd "npm test" [--with-tests] [--max-retries 3]
```

Saída: relatório da execução + instruções de settlement (`shepherd run select/apply/discard <ref>`).

## Tratamento de erros

- Worker travado → `timeout` combinator (default 15 min por tentativa).
- Efeito fora da política → `EffectNotPermitted`; supervisor registra e descarta o fork.
- N tentativas esgotadas → relatório de falha com traces das tentativas; nada aplicado.
- Gate de teste com erro de infra (suite nem roda) → aborta com erro claro, não conta retry.

## Estratégia de teste da própria camada

1. Arcabouço (supervisor, gate, retry, settlement) smoke-testado com o provider
   `static` (determinístico, offline, custo zero) — valida a mecânica sem LLM.
2. Smoke real: 1 feature pequena num repo de brinquedo com Claude, ponta a ponta.
3. Só depois: uso num repo real.

## Fases de entrega

- **F1 (núcleo)**: `tasks.implement` + supervisor com gate de teste + retry + settlement humano + CLI.
- **F2**: `review` ReadOnly + `write_tests` + guidance estruturada nos retries.
- **F3**: 2 workers em paralelo em forks distintos + supervisor coordenando merge
  (réplica completa do experimento CooperBench do paper).

## Fora de escopo (por ora)

- Counterfactual Replay Optimization (aplicação 2 do paper).
- Tree-RL / treino (aplicação 3).
- Auto-apply sem humano no gate.

---

## Adendo F2 (2026-07-11): decisões forçadas pela realidade da 0.3.0

Descobertas empíricas (validadas em 4 workspaces de teste) que alteraram o design:

1. **Reviewer não pode ser syscall-ReadOnly nesta versão.** Lane C (multi-binding)
   exige roots disjuntos (sem sub-root aninhado) E não aceita provider de execução.
   Decisão: reviewer roda na lane single-repo com leitura livre do repo (reviews
   melhores) e isolamento por CUSTÓDIA: output retido nunca aplicado, guard
   determinístico exige changeset == {REVIEW.json}, descarte incondicional após
   leitura do verdict. Propriedade preservada: reviewer não consegue alterar código.

2. **Runs sempre forkam da adoção ORIGINAL do workspace**; select/apply avançam o
   mundo vcscore mas não alimentam o basis dos runs seguintes. Consequências:
   - paths "changed" sem conteúdo no changeset = artefatos de basis, não ações do
     worker → ignorados (worktree é a fonte de verdade);
   - deleção real por worker é inexpressável nesta lane (limitação documentada;
     suporte via effect stream fica para F3);
   - `dev.py run` recria o `.vcscore` a cada invocação (substrato stateless por
     rodada; git é a verdade durável), recusando se houver proposta pendente.

3. **Settlement que materializa arquivos não existe no framework** (select/apply só
   movem o mundo interno). `dev.py settle <ref>` faz: snapshot do changeset →
   `select` (registro de custódia) → espelha arquivos no worktree APÓS fechar o
   workspace (vcs-core bloqueia mutação com workspace ativo). Commit no git fica
   com o humano. Guard anti path-traversal na materialização.

4. **Budget do worker**: campos `budget`/`timeout` são reservados na API pública e
   o provider Claude trava em 240s. `--worker-budget` (default 900s) reergue via
   rebind do seam interno de transporte (workaround alpha; revisar em upgrade).

5. **Recomendações operacionais**: `.vcscore/` e `REVIEW.json` no .gitignore dos
   repos alvo; nunca usar verbos crus `shepherd run select/apply` fora do
   `dev.py settle` (dessincroniza mundo × worktree).

---

## Adendo F3 (2026-07-11): workers paralelos coordenados

Implementado em `parallel.py` + subcomandos `dev.py run2` / `dev.py settle-par`.

**Arquitetura.** Ativação de workspace é exclusiva por diretório na 0.3.0, então o
paralelismo usa um CLONE efêmero do repo por worker (copytree + `shepherd init` em
tmpdir), executados em threads. Cada worker roda o loop F1/F2 no seu clone com
gate individual desligado (policy-only); o juiz é o GATE COMBINADO sobre a
proposta mesclada. Réplica prática do supervisor de runtime do paper com as
ferramentas equivalentes: contexto do teammate no prompt (inject), rework do
follower sobre a proposta do leader (handoff), descarte de tentativa (discard).

**Fluxo run2:**
1. 2 clones; workers em paralelo, cada um sabendo a feature do outro (interfaces
   compatíveis, sem implementar a parte alheia).
2. Conflito = interseção de paths das duas propostas. Havendo conflito: handoff —
   novo clone com a proposta do leader APLICADA; follower re-implementa por cima.
3. Gate combinado (worktree + proposta mesclada). Falha → rodadas de reparo
   (`--max-repairs`, default 2): worker de reparo num clone semeado com tudo,
   guiado pelo tail do erro.
4. Review F2 do diff combinado.
5. Proposta staged em `.shepherd-proposals/<id>/` (files/ + manifest.json com
   runs, conflitos, gate, verdict). `settle-par <id>` escreve no worktree e
   remove o staging; `--reject` descarta. Commit git fica com o humano.

**Validação:** offline static (disjunto, conflito+handoff, manifests) ALL-PASS;
real com 2 workers Claude em paralelo (fizzbuzz + greet): ambos passaram, gate
combinado PASS sem reparos, review APPROVED cobrindo as duas features,
settle-par materializou os 2 arquivos e as suites passaram.

**Limitações herdadas:** deleções reais de worker seguem inexpressáveis;
traces dos clones são efêmeros (destruídos com o clone); veto ao vivo por
intents (stream) continua fora — exigiria a lane baixa (Scope/Device), próximo
candidato de evolução.

---

## Adendo CRO/Tree-RL/auto-apply (2026-07-12): fases A-D implementadas

Estudo em `docs/2026-07-12-study-cro-treerl-autoapply.md`. Executado A→B→C→D.

- **A — history store** (`history.py`): cada run/run2/best-of e settlement grava um evento
  JSONL em `~/.shepherd-dev/history/` (env `SHEPHERD_DEV_HISTORY_DIR`)
  best-effort. Nunca bloqueia/quebra um run.
- **B — auto-apply** (`--auto-settle` em run/run2/best-of): auto-accept SÓ com gate PASS +
  review APPROVED (incompatível com `--no-review` e provider static); settle + commit em
  branch isolada `shepherd/<slug>` (nunca a branch atual, nunca push). Detached HEAD/erro
  de git degradam para arquivos-na-worktree com aviso. Decisão marcada `auto=true` no store.
- **C — best-of-N** (`--best-of K`, K=2..4): essência do Tree-RL em inferência sem treino.
  K workers do MESMO estado (clones efêmeros) com sementes de ênfase (neutra/menor-diff/
  robustez/idiomas); gate em todos; review nos que passam; ranking determinístico
  (gate → aprovação → menos issues → menos arquivos → menor diff); vencedor vira staged
  proposal (settle-par). Fork por TENTATIVA (fork de conversa não é expressável com o
  claude CLI — limite documentado). Treino real do Tree-RL: fora de escopo.
- **D — CRO-lite** (`shepherd-dev optimize`): mina o history por modos de falha, pede ao
  meta-otimizador (Claude, default opus) UMA edição de prompt como JSON, valida por
  **replay real** — cada caso re-executado num git worktree pinado no SHA original, em
  subprocesso, com o prompt candidato injetado via `SHEPHERD_DEV_PROMPTS_OVERRIDES`. Aceita
  só se fix set melhora E guard set não regride (`--apply` persiste; default dry-run).
  Custo em tokens reais (sem KV-reuse na lane pública) — fix/guard pequenos (default 3/3).

Prompts viraram dados em `tasks.py` (DEFAULT_PROMPTS + override file). Restrição do
framework que moldou o design: o SOURCE de uma task NÃO pode ter import relativo nem de
módulo do mesmo pacote (só stdlib + shepherd) — por isso os prompts vivem em `tasks.py`,
não num módulo irmão, e o replay injeta o candidato via arquivo de override lido no import.

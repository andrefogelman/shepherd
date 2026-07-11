# Camada de desenvolvimento com AI supervisionado sobre Shepherd

Data: 2026-07-11 · Status: proposta aguardando aprovação do Andre
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
3. Só depois: uso num repo real do Andre.

## Fases de entrega

- **F1 (núcleo)**: `tasks.implement` + supervisor com gate de teste + retry + settlement humano + CLI.
- **F2**: `review` ReadOnly + `write_tests` + guidance estruturada nos retries.
- **F3**: 2 workers em paralelo em forks distintos + supervisor coordenando merge
  (réplica completa do experimento CooperBench do paper).

## Fora de escopo (por ora)

- Counterfactual Replay Optimization (aplicação 2 do paper).
- Tree-RL / treino (aplicação 3).
- Auto-apply sem humano no gate.

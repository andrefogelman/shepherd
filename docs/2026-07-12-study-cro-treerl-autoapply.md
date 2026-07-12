# Estudo: incorporar CRO, Tree-RL e auto-apply ao shepherd-dev

Data: 2026-07-12 · Status: estudo de decisão
Base: shepherd-ai 0.3.0 (restrições empíricas dos adendos F2/F3), paper arXiv 2605.10913,
repo de experimentos `shepherd-agents/shepherd-experiments` (MIT; `exp/cbo/` = CRO,
`exp/mcts-rl/` = Tree-GRPO, `code/` = snapshot congelado do substrato como uv workspace).

---

## 1. Counterfactual Replay Optimization (aplicação 2)

### O que o paper faz
Um meta-otimizador lê traces de execução de variantes do workflow, diagnostica falhas e
emite edits candidatos (mudanças de prompt/workflow), cada um pareado com um **fix set**
(exemplos que deve consertar) e um **guard set** (que não pode regredir). Cada edit é
validado forkando o trace no primeiro commit afetado e re-executando SÓ o sufixo
(~95% de reuso de KV-cache torna isso barato). Resultado: melhor em 4/5 benchmarks com
27-58% menos wall-clock que os baselines.

### O que otimizaria no shepherd-dev
Nossos "assets de workflow": docstring-prompts das tasks (implement/write_tests/review),
templates de guidance, defaults de policy. São poucos e vivem no nosso repo — edits podem
chegar como proposta do próprio shepherd-dev (gated + settled).

### Bloqueios na 0.3.0 (verificados)
- Lane pública (workspace) expõe trace mínimo (`run.lifecycle`); sem replay entre runs
  (runs forkam da adoção original; settlements não alimentam basis).
- Replay byte-idêntico com reuso de KV existe só na lane baixa (`shepherd_runtime.Scope`
  fork/checkpoint/restore + `Stream`), não documentada para o provider claude; o paper
  usou snapshot próprio do substrato.

### Opções
- **1A. CRO-fiel** (portar `exp/cbo/` na lane baixa/snapshot vendorado): alta
  complexidade, wiring de provider desconhecido, e o ganho é limitado — nosso workflow
  tem ~3 prompts, não um pipeline de 10 estágios como os benchmarks.
- **1B. CRO-lite (recomendado):**
  1. **History store** (pré-requisito): persistir cada DevReport em JSONL
     (`~/.shepherd-dev/history/`) + página GBrain — feature, repo, SHA do commit,
     tentativas, veredictos, tails de gate, issues do reviewer, guidance usada.
  2. `shepherd-dev optimize`: meta-otimizador (Claude, melhor modelo) clusteriza modos
     de falha do histórico e propõe edits concretos aos prompts/templates.
  3. Validação replay-lite: **fix set** = re-rodar N features que falharam, pinadas nos
     SHAs originais, com o prompt editado; **guard set** = re-rodar M que passaram.
     Aceita o edit só se fix melhora E guard não regride.
  4. Custo honesto: sem KV-reuse, re-runs custam tokens reais. Mitigação: N/M pequenos
     (3-5), e mudanças puras de gate/policy validam grátis re-gateando changesets retidos.

---

## 2. Tree-RL / treino (aplicação 3)

### O que o paper faz
Treino GRPO onde um meta-agente escolhe o turno de fork em cada rollout; K=4 irmãos são
amostrados DAQUELE estado exato (barato via fork do substrato); crédito em dois níveis
(prefixo inter-root, sufixo intra-árvore) dá credit assignment por passo sem value model.
Treinaram Qwen3.5-35B e Nemotron-3 via tinker; ~dobra o uplift do GRPO.

### Avaliação honesta de encaixe
É uma aplicação de TREINO de modelo próprio. Exige: modelo open-weights, infra de GPU
(HF Jobs/TRL cobririam), milhares de rollouts, e — o gap decisivo — **controle do estado
de sessão do modelo de política** para forkar mid-conversation. Com o claude CLI
(assinatura) não há fork de sessão; seria preciso servir um modelo aberto (vLLM) sob
nosso controle. Um coder 35B treinado chega a ~39% no Terminal-Bench — muito abaixo do
que os seus planos (Claude/Grok/GLM) já entregam para o trabalho real. Qualidade técnica
para o SEU objetivo (dev melhor nos seus repos): baixa. Projeto de pesquisa, não incremento.

### Opções
- **2A. Treino real: não recomendado agora.** Se um dia houver caso de uso de modelo
  próprio, o caminho é HF Jobs + TRL (GRPO já existe no TRL) + ambiente de tasks — revisitar.
- **2B. Essência incorporável SEM treino (recomendado): `--best-of K` em inferência.**
  O que o Tree-RL explora — branch K do mesmo estado e comparação entre irmãos — vira
  feature de runtime: K workers em clones idênticos (mesmo estado de repo, variações de
  ênfase no prompt), gate em todos, ranking determinístico (gate pass → verdict do
  reviewer → menor diff), o melhor fica retido para settlement e os demais viram
  evidência no relatório. Reusa diretamente a maquinaria de clones do F3. Nossa
  granularidade de fork é por TENTATIVA (não por turno de conversa) — é o que a lane
  atual permite expressar com honestidade.
- 2C. Investigação futura (sem compromisso): fork de conversa via `claude --resume`
  duplicado poderia aproximar fork por turno; frágil, só explorar se 2B mostrar teto.

---

## 3. Auto-apply sem humano no gate

### Design recomendado (opt-in, guardrails duros)
Flag `--auto-settle` em `run` e `run2`:
- **Condições para auto-accept (todas):** gate PASS + review APPROVED (review vira
  obrigatório: `--auto-settle` incompatível com `--no-review`) + policy limpa + zero
  conflitos pendentes (run2). Qualquer critério falhou → fica retido como hoje.
- **Ação:** settle + escrever arquivos + **commit automático em branch isolada**
  `shepherd/<slug>-<run-ref>` (nunca na branch atual; **push nunca automático**).
  Relatório com diff stat e a branch criada.
- **Auditoria:** entrada no history store (item 1B.1) marcada como decisão automática.
- **Risco residual e mitigação:** o reviewer é LLM e pode aprovar bug sutil — mitigado
  por (a) branch isolada = revert trivial, (b) a suite do repo é o árbitro principal,
  (c) `--allowed-prefix` recomendado em modo auto, (d) deleções são impossíveis por
  construção na 0.3.0.
- Extensão natural (fora do escopo inicial): modo fila — lista de features processada
  em sequência auto-settled na mesma branch (CI de features).

---

## Sequência recomendada (dependências, não prazos)

| Fase | Entrega | Depende de | Complexidade |
|---|---|---|---|
| A | History store (JSONL + GBrain) | — | baixa |
| B | Auto-apply (`--auto-settle` + branch + commit) | A (auditoria) | baixa |
| C | Best-of-N (`--best-of K`) | maquinaria F3 (pronta) | média |
| D | CRO-lite (`shepherd-dev optimize`, fix/guard sets) | A + histórico acumulado | alta |
| — | Tree-RL treino real | fora de recomendação; revisitar com caso de uso | pesquisa |

Racional da ordem: A é fundação de B e D e tem valor imediato (auditoria/telemetria);
B destrava fluxo contínuo com o menor esforço; C reusa o F3 e melhora qualidade por
run; D é o mais caro e fica melhor quanto mais histórico houver acumulado.

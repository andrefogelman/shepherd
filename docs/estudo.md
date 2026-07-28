# Estudo: o modelo de proposta chegou ao teto — patch git como candidato

Data: 2026-07-28. Origem: análise das limitações de desenho documentadas no
código (não bugs abertos), com a pergunta: há alteração estrutural que valha
estudar sem quebrar funcionalidade nem princípios?

## As 6 limitações (todas decorrem de UMA escolha de modelo)

1. **Deleções não entram na proposta.** O pipeline de entries só carrega
   arquivos presentes (path → bytes). Delete do worker não vira entrada no
   changeset nem é reaplicado no gate/settle. Vale nas duas lanes (workspace e
   hosted). No código: limitação da lane v0.3.0; effect-stream seria F3.
2. **Artefatos stale do basis são ignorados (lane git).** Runs forcam do basis
   original de adoção; `changed_paths` pode listar paths cujo conteúdo não está
   legível (`read_file → None`), pulados de propósito. Efeito colateral:
   reforça que delete real não se expressa nessa lane.
3. **Mode-only sem baseline (lane hosted).** Com baseline (caminho de
   produção), chmod-only entra. Sem baseline, a comparação é só conteúdo e o
   mode-only some. Todo caller de produção passa baseline.
4. **Exec bit vive em atributo frágil (`Entries`).** O tipo continua
   `dict[str, bytes]`; o bit vai em `.executable`. `dict(entries)` ou
   `{**a, **b}` apagam o atributo em silêncio; merges precisam de
   `as_entries()`. Não é bug se o pipeline usa `as_entries`; é footgun de API
   para código novo.
5. **Só o bit de execução, não o filemode completo.** Preserva-se "tem x?"
   (reaplicado como 0o755/0o644). Symlink e demais modes não viajam.
6. **Symlinks e ruído de árvore fora do diff hosted.** `_walk` ignora symlinks
   e dirs tipo .git/.venv/node_modules. Proposta hosted = arquivos regulares
   sob o que o walker enxerga.

**Diagnóstico em uma frase:** o sistema modela proposta como *mapa de conteúdo
de arquivos* (com +x opcional), não como *patch git* — por isso delete, mode
completo e cópias ingênuas do dict ficam de fora ou exigem cuidado.

## A sugestão estrutural: proposta como patch git, não como dict

O histórico mostra o padrão: cada atributo novo vira um sidecar no dict. O
exec bit virou `.executable` (com o footgun #4). Deleção seria um segundo
sidecar (`.deleted`) com o mesmo footgun; rename, um terceiro. Cada um exige
caça a `dict(...)`/`{**a, **b}` em ~130 pontos. O modelo paralelo cobra mais
do que entrega — e o git já tem a álgebra completa: diff, apply, 3-way,
modes, deleções, renames, symlinks, com semântica de conflito.

**Desenho a estudar:** uma proposta aprovada vira um artefato git real
(patch/commit num ref temporário) e uma única representação canônica serve:

- gate local — `git apply` na worktree staged (substitui `materialize_into`);
- gate remoto — envia o patch em vez do tar de conteúdos (menor, mode-aware;
  substitui `_tar_entries`);
- stage/settle — `git apply`/`am` (substitui o re-read do settle-par);
- `Entries`/`as_entries` e o footgun #4 morrem junto.

**Onde é estritamente melhor, não só equivalente:**

- *#2/#3 (basis/stale):* `git apply --3way` contra o basis do snapshot
  reporta conflito EM VOZ ALTA quando a árvore driftou. Hoje o modelo de
  conteúdo escolhe em silêncio (skip sem registro; o re-gate do settle-par
  mitiga, mas tarde). Conflito explícito é a filosofia do repo: closure on
  evidence, never on silence.
- *#1 (deleções):* de graça no formato. A lane hosted já tem o insumo
  (snapshot tem, walk não tem → tombstone). Quem precisa do F3/effect-stream
  é a lane git, onde "deletado pelo worker" e "artefato stale do basis" são
  indistinguíveis na leitura.

## Sequenciamento (a parte importante)

**Não fazer agora.** As 6 limitações estão documentadas, as premissas
intactas, e nenhuma tem requisito forcando. O gatilho natural é o primeiro
caso real de deleção (ou o F3 do substrato): aí o carrier precisa de um tipo
de elemento novo de qualquer jeito, e a pergunta "crescer o dict ou adotar o
formato do git" se impõe com evidência. Migrar antes é refatoração por
higiene — o que o repo evita.

Ordem quando o gatilho chegar: lane hosted primeiro (o diff por snapshot já
emite tudo que um patch precisa); lane git quando o substrato expuser
deleções.

## O que NÃO estudar

- **Tombstone como valor-sentinela no dict** (`entries[rel] = DELETED`):
  infecta todos os leitores com caso especial e continua sem a álgebra do
  git. Pior dos dois mundos.
- **Symlinks como entries first-class (#6):** o skip atual é postura de
  segurança (#18, tar-slip), não só limitação. Benefício pequeno, superfície
  de ataque real.
- **`Entries` virar dataclass só pelo footgun #4:** quebra ~130 leitores por
  higiene de tipo. Se a migração para patch acontecer, o footgun morre junto.
  Alternativa barata no idioma do repo: check estático (estilo
  `test_undefined_names`) proibindo `dict(...)`/`{**...}` sobre `Entries`
  fora do `as_entries` — "decidir sem executar". Frágil contra aliases, mas
  quase grátis.

## Item pequeno, independente do estudo

Do #2: o skip de `read_file → None` é silencioso. Contar e emitir ("N paths
stale-basis ignorados") no relatório/event log não muda semântica e converte
decisão invisível em evidência. É o único ponto da lista considerável ANTES
de qualquer gatilho, porque é observabilidade, não modelo.

## Conclusão

Admitir que o dict chegou ao teto e marcar a migração para patch git como o
desenho candidato **quando deleção virar requisito**. Estudá-la antes disso é
gastar a refatoração sem o requisito que a justifica.

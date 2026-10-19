---
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Bash, Write, Read, Skill]
runs: 3
---
Primeiro monte um repositório de teste em `./repo`:
- `git init`
- `csvparse.py` com `parse(text, delimiter=",")` que implementa CSV RFC 4180 (campos entre aspas, aspas escapadas `""`, CRLF, campos multilinha entre aspas, delimitador customizado), retornando lista de listas de strings
- um `test_smoke.py` com um único teste `unittest` trivial, `self.assertTrue(True)`, sem importar `csvparse`
- commit inicial.

Depois: com o shepherd, só escreve os testes unitários do parser CSV (cobrindo cada caso de borda acima). Não mexe no código de produção.

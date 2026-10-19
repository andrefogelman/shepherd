---
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Bash, Write, Read, Skill]
runs: 3
---
Primeiro monte um repositório de teste em `./repo`:
- `git init`, `package.json` com `{"type":"module","scripts":{"test":"node --test"}}`
- `cnpj.js` exportando `validaCnpj(s)` (valida os dígitos verificadores do CNPJ)
- `cnpj.test.js` com 3 testes usando `node:test` e `node:assert`
- commit inicial.

Depois: desenvolve com o shepherd, nesse repo, um validador de CEP brasileiro (8 dígitos, aceita com ou sem hífen), puro e no mesmo estilo do validador de CNPJ existente, com testes.

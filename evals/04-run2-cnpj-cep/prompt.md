---
max_turns: 50
timeout_seconds: 2400
allowed_tools: [Bash, Write, Read, Skill]
runs: 3
---
Primeiro monte um repositório de teste em `./repo`:
- `git init`, `package.json` com `{"type":"module","scripts":{"test":"node --test"}}`
- `index.js` exportando `export const version = "1.0.0";`
- `index.test.js` com um teste `node:test` que verifica `version`
- commit inicial.

Depois: roda dois workers paralelos supervisionados, coordenados, nesse repo:
A) um validador puro de CNPJ (dígitos verificadores), com testes;
B) um validador puro de CEP brasileiro (8 dígitos, com ou sem hífen), com testes.

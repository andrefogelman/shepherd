---
type: regex
target: {source: file, path: repo/cnpj.js}
match: not_contains
---
\bcep|Cep|CEP(?![A-Z])

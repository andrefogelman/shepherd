---
type: regex
target: {source: file, path: repo/cnpj.test.js}
match: not_contains
---
\bcep|Cep|(?<![A-Z])CEP|CEP(?![A-Z])

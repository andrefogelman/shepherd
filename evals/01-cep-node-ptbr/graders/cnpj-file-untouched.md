---
type: regex
target: {source: file, path: repo/cnpj.js}
match: not_contains
---
\bcep|Cep|(?<![A-Z])CEP|CEP(?![A-Z])

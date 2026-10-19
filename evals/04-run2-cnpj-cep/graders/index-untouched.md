---
type: regex
target: {source: file, path: repo/index.js}
match: not_contains
---
\bcep|Cep|CEP(?![A-Z])|cnpj|Cnpj|CNPJ

---
type: regex
target: {source: file, path: repo/index.test.js}
match: not_contains
---
\bcep|Cep|CEP(?![A-Z])|cnpj|Cnpj|CNPJ

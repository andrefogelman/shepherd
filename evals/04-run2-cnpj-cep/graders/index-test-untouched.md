---
type: regex
target: {source: file, path: repo/index.test.js}
match: not_contains
---
\bcep|Cep|(?<![A-Z])CEP|CEP(?![A-Z])|cnpj|Cnpj|CNPJ

---
type: regex
target: {source: file, path: repo/index.js}
match: not_contains
---
\bcep|Cep|(?<![A-Z])CEP|CEP(?![A-Z])|cnpj|Cnpj|CNPJ

---
type: regex
target: {source: file, path: repo/cnpj.test.js}
match: not_contains
---
(?<![a-z])cep|cep(?![a-zç])|Cep|(?<![A-Z])CEP|CEP(?![A-Z])

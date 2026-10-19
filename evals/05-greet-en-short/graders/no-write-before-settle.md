---
type: regex
target: files
match: not_contains
flags: mi
---
^repo/(?!\.|[^\n]*/\.)(?![^\n]*\.pyc$)(?!test_hello\.py$)[^\n]*(?:hello|greet)[^\n]*$

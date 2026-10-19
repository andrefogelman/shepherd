---
type: regex
target: files
match: not_contains
flags: mi
---
^repo/(?:[^./\n][^/\n]*/)*(?!test_smoke\.py$)[^/\n]*test[^/\n]*\.py$

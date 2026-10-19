---
type: regex
target: {source: file, path: repo/test_hello.py}
match: not_contains
---
def\s+greet\b|(^|\n)[ \t]*greet\s*=

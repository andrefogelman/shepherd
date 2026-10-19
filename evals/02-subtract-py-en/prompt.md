---
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Bash, Write, Read, Skill]
runs: 3
---
First set up a test repo in `./repo`:
- `git init`
- `calc.py` with `def add(a, b): return a + b`
- `test_calc.py` with one `unittest` test for `add`
- initial commit.

Then: use a supervised sandboxed worker with a test gate to add a `subtract(a, b)` function to `calc.py` with a unit test in `test_calc.py`. I want to approve the change before any of my files are modified.

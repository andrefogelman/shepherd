---
max_turns: 40
timeout_seconds: 1800
allowed_tools: [Bash, Write, Read, Skill]
runs: 3
---
Set up `./repo`: `git init`, a `test_hello.py` with a `unittest` test that imports `greet` from `hello` and asserts it returns `"hi"`, and an initial commit.

Then develop this feature with shepherd: `hello.py` with a function `greet()` that returns exactly the string `hi`.

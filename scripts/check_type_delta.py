"""Fail on new Pyright errors relative to a specified repository baseline.

Line numbers intentionally do not enter the comparison: adding a helper moves
existing diagnostics. Multiplicity does enter it, so repeating an old error at
a new site still fails. Both checkouts use one interpreter and configuration.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def diagnostics(root: Path):
    with tempfile.TemporaryDirectory(prefix="shepherd-pyright-") as tmp:
        config = Path(tmp) / "pyrightconfig.json"
        config.write_text(json.dumps({
            "pythonVersion": "3.11",
            "typeCheckingMode": "basic",
            "reportUnusedParameter": False,
        }))
        proc = subprocess.run(
            ["pyright", "--project", str(config), "--pythonpath", sys.executable,
             "--outputjson", str(root / "src")],
            cwd=root, capture_output=True, text=True, timeout=180,
        )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"pyright failed: {proc.stderr}\n{proc.stdout}")
    result = json.loads(proc.stdout)
    if "generalDiagnostics" not in result:
        raise RuntimeError(f"pyright did not report diagnostics: {result}")
    analyzed = result.get("summary", {}).get("filesAnalyzed", 0)
    expected = sum(1 for _ in (root / "src").rglob("*.py"))
    if not expected or analyzed < expected:
        raise RuntimeError(f"pyright analyzed {analyzed} files; expected at least {expected}")
    errors = collections.Counter()
    for item in result["generalDiagnostics"]:
        if item["severity"] == "error":
            path = Path(item["file"]).relative_to(root).as_posix()
            # Root paths can also occur in type messages (e.g. import errors).
            message = item["message"].replace(str(root), "<checkout>")
            errors[(path, item.get("rule", ""), message)] += 1
    print(f"{root}: {analyzed} files analyzed, {sum(errors.values())} type errors", flush=True)
    return errors


def main():
    baseline, current = (Path(p).resolve() for p in sys.argv[1:])
    before = diagnostics(baseline)
    after = diagnostics(current)
    introduced = after - before
    for (path, rule, message), count in sorted(introduced.items()):
        print(f"{path}: {rule}: {message} (new occurrences: {count})")
    print(f"New type errors: {sum(introduced.values())}")
    return bool(introduced)


if __name__ == "__main__":
    sys.exit(main())

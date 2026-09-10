"""Static checks over code the offline suite cannot execute.

The live suite runs only against a real model, so a typo or a missing import in
it surfaces minutes into a run on someone else's machine rather than in CI.
Three such errors reached a live run during development — an undefined helper, a
missing import, a name defined only in conftest — and none was reachable by any
test.

pyflakes catches all three in milliseconds. It is not a substitute for running
the code; it is the cheapest available guard on code that is expensive to run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECKED = ["tests/live", "packages/datadirector/src", "scripts",
           "packages/datadirector-contracts/src"]

# Package facades re-export names for callers, which pyflakes reads as unused
# imports. Excluded rather than annotated: the alternative is a noqa on every
# line of a file whose entire purpose is re-export.
EXCLUDE = ("__init__.py",)


def _pyflakes_available() -> bool:
    return subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _pyflakes_available(),
                    reason="pyflakes not installed (pip install pyflakes)")
@pytest.mark.parametrize("target", CHECKED)
def test_no_undefined_names_or_unused_imports(target):
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", target],
        cwd=ROOT, capture_output=True, text=True)
    findings = [line for line in result.stdout.splitlines()
                if line.strip() and not any(x in line for x in EXCLUDE)]
    assert not findings, (
        f"pyflakes findings in {target}:\n" + "\n".join(findings)
    )

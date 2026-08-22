"""
The signup page previews a creator's workspace address before they submit.

That preview is computed in JavaScript (`static/js/auth.js`) and the real slug
is computed in Python (`services/accounts.py`). **Two implementations of one
rule is a drift risk**, and drift here is not cosmetic: a creator sees one web
address and gets another, at exactly the moment they are deciding whether to
trust us.

This test runs the SHIPPED JavaScript — extracted from the file the browser
actually downloads, not a copy — against the Python, over the inputs where the
two are most likely to disagree.

Skipped, not failed, when Node is unavailable: our CI is Python-only, and a
test that cannot run must be visibly absent rather than quietly green.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.accounts import slugify

JS_PATH = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "auth.js"

#: Chosen for disagreement, not coverage. Each one is a rule the two
#: implementations state separately and could state differently.
CASES = [
    "Zuma Mitumba Bales",  # the ordinary case
    "Nairobi Thrift",
    "  leading and trailing  ",
    "Café Nairobi",  # NFKD: accent stripped, letter kept
    "Züri Störe",
    "M-Pesa   Shop",  # collapsing runs of separators
    "shop!!!name???",
    "---dashes---",  # stripped from both ends
    "UPPER Case Name",
    "Shop 254 Kenya",
    "ONE",  # too short for the server, but the preview still renders it
    "a" * 60,  # MAX_SLUG_LENGTH truncation
    "x" * 39 + " y",  # truncation landing on a separator → trailing dash
    "🔥 fire shop 🔥",  # non-ASCII dropped entirely
    "Ходить",  # nothing ASCII survives; both must return ""
    "",
]

requires_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="Node is not installed — JS/Python slug parity cannot be checked",
)


def extract_slugify() -> str:
    """
    Pull the ``slugify`` function out of the shipped auth.js.

    Reading the real file rather than keeping a copy here is the whole point: a
    copy would drift alongside the original and prove nothing.

    Returns:
        The function's source, ready to evaluate in Node.

    Raises:
        AssertionError: If the function cannot be found, which means it was
            renamed or removed and this test needs updating rather than
            silently passing over nothing.
    """
    source = JS_PATH.read_text(encoding="utf-8")
    start = source.find("function slugify(")
    assert start != -1, f"slugify() not found in {JS_PATH.name}"

    # Walk the braces to the end of the function body.
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]

    raise AssertionError("unbalanced braces in slugify()")


@requires_node
def test_the_javascript_preview_matches_the_python_slug(tmp_path: Path) -> None:
    """Every case must produce a byte-identical slug on both sides."""
    script = tmp_path / "parity.mjs"
    script.write_text(
        f"{extract_slugify()}\n"
        f"const cases = {json.dumps(CASES)};\n"
        "console.log(JSON.stringify(cases.map(slugify)));\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    from_js = json.loads(completed.stdout)
    from_python = [slugify(case) for case in CASES]

    mismatches = [
        (case, js, py) for case, js, py in zip(CASES, from_js, from_python, strict=True) if js != py
    ]
    assert not mismatches, (
        "auth.js and services/accounts.py disagree about the slug. "
        f"Mismatches (input, js, python): {mismatches}"
    )

"""The anti-overfitting guarantee, checked rather than claimed.

"How do you know this isn't tuned to the data you were given?" is the question
this project has to answer, and until now the answer was a 120-line synthetic
fixture: point the tool at a payments pipeline and watch it work. That proved the
tool works on *one* other system.

This proves something stronger and costs 30 lines: no executable line of the tool
mentions anything about this export. Not a service, not a channel, not the name of
the field that ties records together. Comments and docstrings may discuss the data
freely — that's where the reasoning lives, and hiding it would make the code worse.
Code may not depend on it.

Two real leaks were found the first time this ran, and both mattered:

  * `cli.py` stripped the literal prefix "comms-" for display — cosmetic, but it
    is exactly the kind of thing that accumulates until the tool only reads well
    against one export.
  * `tools.py` used {"message_type": "push"} as the example in a tool schema,
    which goes into the *prompt*. The model was being taught this pipeline's
    vocabulary as though it were general.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "tracelens"

VOCABULARY = (
    "email",
    "sms",
    "push",
    "comms-",
    "message_type",
    "correlation_id",
    "tenant_id",
    "sqs",
    "sns",
    "sendgrid",
    "pinpoint",
    "corr-",
)


def docstring_lines(tree: ast.AST) -> set[int]:
    """Line numbers occupied by docstrings, which are exempt."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return lines


def executable_lines(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    exempt = docstring_lines(ast.parse(source))
    return [
        (number, line.split("#", 1)[0])
        for number, line in enumerate(source.splitlines(), 1)
        if number not in exempt
    ]


@pytest.mark.parametrize("path", sorted(SOURCE.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_depends_on_this_export(path):
    offences = [
        f"{path.name}:{number} mentions {word!r} — {line.strip()[:80]}"
        for number, line in executable_lines(path)
        for word in VOCABULARY
        if word in line
    ]
    assert not offences, "\n".join(offences)


def test_the_check_would_actually_catch_something(tmp_path):
    """A guard that cannot fail is worse than no guard. This is the test for the
    test — it was written after realising the first version silently exempted
    every string literal, which is where both real leaks lived."""
    leaky = tmp_path / "leaky.py"
    leaky.write_text(
        '"""A docstring mentioning message_type is fine."""\n'
        'CHANNELS = ["email", "sms"]  # this is not\n',
        encoding="utf-8",
    )
    lines = executable_lines(leaky)
    assert not any("message_type" in line for _, line in lines), "docstring not exempt"
    assert any("email" in line for _, line in lines), "code line not checked"

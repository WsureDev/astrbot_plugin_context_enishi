from __future__ import annotations

import ast
from pathlib import Path


def test_functional_domains_do_not_import_each_other():
    root = Path(__file__).parents[1]
    for directory, forbidden in (
        ("context", {"awareness"}),
        ("awareness", {"context"}),
        ("channels", {"awareness", "context"}),
    ):
        for path in (root / directory).glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split(".")[0] not in forbidden, str(path)

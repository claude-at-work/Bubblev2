"""Python import scanner (AST-based).

Stage 1 of the pipeline:  scan(script_path) -> ImportSet
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path


# sys.stdlib_module_names is the authoritative set on 3.10+. Don't drift.
STDLIB: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())


# Python import name → PyPI distribution name. Common cases only — extend as needed.
IMPORT_TO_DIST = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "Cryptodome": "pycryptodomex",
    "Crypto": "pycryptodome",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "git": "GitPython",
    "magic": "python-magic",
    "OpenSSL": "pyOpenSSL",
    "dotenv": "python-dotenv",
    "socks": "PySocks",
    "Levenshtein": "python-Levenshtein",
    "serial": "pyserial",
    "usb": "pyusb",
    "lxml": "lxml",
    "attr": "attrs",
}


@dataclass
class ImportSet:
    script: Path
    top_level_imports: set[str] = field(default_factory=set)
    stdlib_imports: set[str] = field(default_factory=set)
    raw_imports: set[str] = field(default_factory=set)  # full dotted paths

    @property
    def candidate_distributions(self) -> set[str]:
        """Top-level imports mapped to PyPI distribution names."""
        return {IMPORT_TO_DIST.get(t, t) for t in self.top_level_imports}


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self.imports.add(node.module)


def scan(script_path: Path) -> ImportSet:
    """Walk the AST of a script, collect imports, classify into stdlib/external."""
    script_path = Path(script_path)
    source = script_path.read_text(errors="replace")
    try:
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError as exc:
        raise ValueError(f"could not parse {script_path}: {exc}")

    visitor = _Visitor()
    visitor.visit(tree)

    out = ImportSet(script=script_path)
    out.raw_imports = set(visitor.imports)
    for full in visitor.imports:
        top = full.split(".")[0]
        if top in STDLIB:
            out.stdlib_imports.add(top)
        else:
            out.top_level_imports.add(top)
    return out

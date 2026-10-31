"""Installed-package and standalone-boundary checks."""

import ast
import importlib.metadata
from pathlib import Path

import agent_harness


def test_installed_package() -> None:
    assert importlib.metadata.version("agent-harness") == "0.1.0"
    assert agent_harness.__file__ is not None


def test_standalone_import_boundary() -> None:
    assert agent_harness.__file__ is not None
    package = Path(agent_harness.__file__).parent
    forbidden = {"aios", "cerebrum"}
    for source in package.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {name.split(".")[0].lower() for name in names} & forbidden, source
    # There are no runtime dependencies in the foundation. Development extras are optional.
    requirements = importlib.metadata.requires("agent-harness") or []
    assert all(
        "extra == 'dev'" in requirement or 'extra == "dev"' in requirement
        for requirement in requirements
    )

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
    # Phase 2 adds validation, local HTTP and CLI dependencies.
    requirements = importlib.metadata.requires("agent-harness") or []
    runtime = [requirement for requirement in requirements if "extra ==" not in requirement]
    assert {requirement.split("<")[0].split(">=")[0] for requirement in runtime} == {
        "aiosqlite",
        "httpx",
        "pydantic",
        "typer",
        "jsonschema",
    }


def test_domain_and_port_import_boundaries() -> None:
    assert agent_harness.__file__ is not None
    package = Path(agent_harness.__file__).parent
    import sys

    for layer in ("domain", "ports"):
        for source in (package / layer).rglob("*.py"):
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.level == 0, "Use explicit imports so boundaries remain auditable"
                    names = [node.module]
                for name in names:
                    assert name.split(".")[0] in sys.stdlib_module_names or name.startswith(
                        "agent_harness.domain."
                    ), (source, name)

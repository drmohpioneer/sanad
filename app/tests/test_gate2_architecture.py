"""Gate 2 dependency rules for channel and command seams.

The checks in this file deliberately inspect Python syntax rather than source
text.  Comments, docstrings, formatting, and import aliases therefore cannot
create either a false violation or a false green.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
CORE = APP_ROOT / "core"

# These are composition/provider edges, not domain modules.  Keeping the list
# exact makes a new exception a reviewed architecture change rather than a
# filename convention that any future module can opt into.
EDGE_MODULES = frozenset(
    {
        "core.adapters",
        "core.adapter_registry",
        "core.telegram",
        "core.tg_router",
    }
)

COMMAND_BUS_ALLOWED_CORE = frozenset({"channel_contracts"})


@dataclass(frozen=True)
class ImportRecord:
    line: int
    kind: str
    base: str
    members: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.base, *self.members)))


@dataclass(frozen=True)
class DynamicImport:
    line: int
    function: str
    target: str | None


def _module_name(path: Path) -> str:
    relative = path.relative_to(APP_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_files() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in sorted(CORE.rglob("*.py"))
    }


def _package_for(module: str, path: Path) -> str:
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _from_base(node: ast.ImportFrom, module: str, path: Path) -> str:
    if node.level == 0:
        return node.module or ""
    relative = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(relative, _package_for(module, path))
    except (ImportError, ValueError):
        # Invalid relative imports still receive a deterministic diagnostic;
        # importing the module itself will report the syntax/runtime error.
        return relative


def _imports(path: Path, module: str) -> tuple[ImportRecord, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    records: list[ImportRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            records.extend(
                ImportRecord(node.lineno, "import", alias.name)
                for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, module, path)
            members = tuple(
                f"{base}.{alias.name}" if base else alias.name
                for alias in node.names
                if alias.name != "*"
            )
            records.append(ImportRecord(node.lineno, "from-import", base, members))
    return tuple(records)


def _literal_string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _dynamic_imports(path: Path, module: str) -> tuple[DynamicImport, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    importlib_aliases = {"importlib"}
    import_module_aliases: set[str] = set()
    builtin_import_aliases = {"__import__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "builtins":
            for alias in node.names:
                if alias.name == "__import__":
                    builtin_import_aliases.add(alias.asname or alias.name)

    found: list[DynamicImport] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = ""
        if isinstance(node.func, ast.Name):
            if node.func.id in builtin_import_aliases or node.func.id in import_module_aliases:
                function = node.func.id
        elif isinstance(node.func, ast.Attribute):
            if node.func.attr == "__import__":
                function = "__import__"
            elif (
                node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_aliases
            ):
                function = f"{node.func.value.id}.import_module"
        if not function:
            continue

        target = _literal_string(node.args[0]) if node.args else None
        if target and target.startswith("."):
            package_node: ast.AST | None = node.args[1] if len(node.args) > 1 else None
            for keyword in node.keywords:
                if keyword.arg == "package":
                    package_node = keyword.value
            package = _literal_string(package_node)
            if package:
                try:
                    target = importlib.util.resolve_name(target, package)
                except (ImportError, ValueError):
                    pass
        found.append(DynamicImport(node.lineno, function, target))
    return tuple(found)


def _provider_target(target: str) -> bool:
    return any(
        segment.lower().startswith(("telegram", "whatsapp"))
        for segment in target.lstrip(".").split(".")
        if segment
    )


def _stdlib(target: str) -> bool:
    top = target.lstrip(".").split(".", 1)[0]
    return top in sys.stdlib_module_names


def _stdlib_or_pydantic(target: str) -> bool:
    top = target.lstrip(".").split(".", 1)[0]
    return _stdlib(target) or top == "pydantic"


def _registry_dependency(target: str) -> bool:
    return _stdlib(target) or target == "core.channel_contracts" or target.startswith(
        "core.channel_contracts."
    )


def _record_violations(
    record: ImportRecord,
    allowed,
) -> tuple[str, ...]:
    """Return the concrete dependencies a from-import leaves disallowed."""
    if allowed(record.base):
        return ()
    allowed_members = tuple(target for target in record.members if allowed(target))
    if record.members and len(allowed_members) == len(record.members):
        return ()
    if record.members:
        return tuple(target for target in record.members if not allowed(target))
    return (record.base,)


def _command_bus_forbidden(target: str) -> bool:
    clean = target.lstrip(".")
    if _provider_target(clean):
        return True
    if clean == "main" or clean.startswith("main.") or clean == "app.main" or clean.startswith("app.main."):
        return True
    parts = clean.split(".")
    if parts and parts[0] == "core":
        parts = parts[1:]
    head = parts[0] if parts else ""
    if clean.startswith("core."):
        return head not in COMMAND_BUS_ALLOWED_CORE

    # Catch a package-local handler imported as an absolute top-level name as
    # well as the conventional ``core.<module>`` spelling.  The discovered set
    # makes a newly added specialist forbidden without maintaining a second
    # allowlist that can silently go stale.
    local_modules = {
        name.split(".")[-1]
        for name in _module_files()
        if name not in {"core", "core.command_bus"}
    }
    return head in local_modules and head not in COMMAND_BUS_ALLOWED_CORE


class DomainProviderBoundary(unittest.TestCase):
    def test_domain_modules_do_not_import_telegram_or_whatsapp(self) -> None:
        modules = _module_files()
        boundary = sorted(name for name in modules if name in EDGE_MODULES)
        domain = sorted(name for name in modules if name not in EDGE_MODULES)
        self.assertTrue(domain, "architecture scan found no domain modules")
        self.assertTrue(boundary, "architecture scan found no channel boundary modules")

        violations: set[str] = set()
        for module in domain:
            path = modules[module]
            relative = path.relative_to(APP_ROOT).as_posix()
            for record in _imports(path, module):
                for target in record.targets:
                    if _provider_target(target):
                        violations.add(
                            f"{relative}:{record.line}: {record.kind} {target}"
                        )
            for call in _dynamic_imports(path, module):
                if call.target is not None and _provider_target(call.target):
                    violations.add(
                        f"{relative}:{call.line}: {call.function}({call.target!r})"
                    )

        self.assertEqual(
            [],
            sorted(violations),
            "domain modules import provider-specific Telegram/WhatsApp code:\n"
            + "\n".join(sorted(violations)),
        )


class Gate2SeamDependencies(unittest.TestCase):
    def _required(self, filename: str) -> tuple[Path, str]:
        path = CORE / filename
        self.assertTrue(
            path.is_file(),
            f"missing Gate 2 module: app/core/{filename}",
        )
        return path, _module_name(path)

    def test_channel_contracts_import_only_stdlib_and_pydantic(self) -> None:
        path, module = self._required("channel_contracts.py")
        violations: list[str] = []
        for record in _imports(path, module):
            for target in _record_violations(record, _stdlib_or_pydantic):
                violations.append(
                    f"core/channel_contracts.py:{record.line}: {record.kind} {target}"
                )
        for call in _dynamic_imports(path, module):
            violations.append(
                f"core/channel_contracts.py:{call.line}: dynamic import via {call.function}"
            )
        self.assertEqual([], sorted(violations), "\n".join(sorted(violations)))

    def test_adapter_registry_imports_only_contracts_and_standard_library(self) -> None:
        path, module = self._required("adapter_registry.py")
        violations: list[str] = []
        for record in _imports(path, module):
            for target in _record_violations(record, _registry_dependency):
                violations.append(
                    f"core/adapter_registry.py:{record.line}: {record.kind} {target}"
                )
        for call in _dynamic_imports(path, module):
            violations.append(
                f"core/adapter_registry.py:{call.line}: dynamic import via {call.function}"
            )
        self.assertEqual([], sorted(violations), "\n".join(sorted(violations)))

    def test_command_bus_has_no_runtime_or_domain_handler_imports(self) -> None:
        path, module = self._required("command_bus.py")
        violations: set[str] = set()
        for record in _imports(path, module):
            for target in record.targets:
                if _command_bus_forbidden(target):
                    violations.add(
                        f"core/command_bus.py:{record.line}: {record.kind} {target}"
                    )
        for call in _dynamic_imports(path, module):
            # Handler injection leaves CommandBus with no legitimate reason to
            # evade its static dependency boundary through a dynamic import.
            violations.add(
                f"core/command_bus.py:{call.line}: dynamic import via {call.function}"
            )
        self.assertEqual(
            [],
            sorted(violations),
            "CommandBus must receive runtime/domain handlers by injection:\n"
            + "\n".join(sorted(violations)),
        )


if __name__ == "__main__":
    unittest.main()

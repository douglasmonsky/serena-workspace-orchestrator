"""Pure routing rules for the hybrid JetBrains/clangd Serena gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


NATIVE_SUFFIXES = frozenset(
    {
        ".c",
        ".h",
        ".i",
        ".ii",
        ".cc",
        ".cpp",
        ".cxx",
        ".c++",
        ".cppm",
        ".ccm",
        ".cxxm",
        ".c++m",
        ".hpp",
        ".hh",
        ".hxx",
        ".h++",
        ".inl",
        ".ipp",
        ".ixx",
        ".tpp",
        ".txx",
        ".ino",
    }
)
ROUTED_TOOLS = {
    "jet_brains_get_symbols_overview": "get_symbols_overview",
    "jet_brains_find_symbol": "find_symbol",
    "jet_brains_find_referencing_symbols": "find_referencing_symbols",
    "jet_brains_find_declaration": "find_declaration",
    "jet_brains_find_implementations": "find_implementations",
    "jet_brains_run_inspections": "get_diagnostics_for_file",
}
DIAGNOSTIC_SEVERITIES = {
    "ERROR": 1,
    "WARNING": 2,
    "WEAK_WARNING": 3,
    "INFO": 3,
}


class RoutingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "unsupported-native-option") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RoutedToolCall:
    backend: str
    tool_name: str
    arguments: dict[str, Any]


def is_native_relative_file(root: Path | str, relative_path: object) -> bool:
    if not isinstance(relative_path, str) or not relative_path:
        return False

    relative = Path(relative_path)
    if relative.is_absolute():
        return False

    try:
        canonical_root = Path(root).resolve(strict=True)
        candidate = (canonical_root / relative).resolve(strict=True)
        candidate.relative_to(canonical_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False

    return candidate.is_file() and candidate.suffix.casefold() in NATIVE_SUFFIXES


def route_tool_call(
    root: Path | str,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> RoutedToolCall:
    copied_arguments = dict(arguments or {})
    target_name = ROUTED_TOOLS.get(tool_name)
    if target_name is None or not is_native_relative_file(
        root, copied_arguments.get("relative_path")
    ):
        return RoutedToolCall("primary", tool_name, copied_arguments)

    if tool_name == "jet_brains_get_symbols_overview":
        if copied_arguments.pop("include_file_documentation", False):
            raise RoutingError("clangd cannot include file documentation in symbol overviews")
    elif tool_name == "jet_brains_find_symbol":
        if copied_arguments.pop("search_deps", False):
            raise RoutingError("clangd file-scoped symbol search cannot search dependencies")
    elif tool_name == "jet_brains_run_inspections":
        copied_arguments = _map_inspection_arguments(copied_arguments)

    return RoutedToolCall("secondary", target_name, copied_arguments)


def _map_inspection_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    mapped = dict(arguments)
    inspection_names = mapped.pop("inspection_names", None)
    if inspection_names:
        raise RoutingError("clangd diagnostics cannot select JetBrains inspections")

    severity = mapped.get("min_severity")
    if severity is None:
        mapped["min_severity"] = 4
    elif isinstance(severity, str) and severity.upper() in DIAGNOSTIC_SEVERITIES:
        mapped["min_severity"] = DIAGNOSTIC_SEVERITIES[severity.upper()]
    else:
        raise RoutingError(f"unsupported native diagnostic severity: {severity!r}")

    mapped["start_line"] = _map_line(mapped.get("start_line"), default=0)
    mapped["end_line"] = _map_line(mapped.get("end_line"), default=-1)
    return mapped


def _map_line(value: object, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RoutingError("native diagnostic line bounds must be positive integers")
    return value - 1

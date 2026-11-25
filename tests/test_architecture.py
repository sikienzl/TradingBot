"""Architecture guard for the ``src`` package.

The checks below are a ratchet, not an aspiration: they encode the layering the
codebase has today so that new work cannot quietly re-introduce the coupling
that made ``CryptoTradingBot`` hard to test.

Rules enforced:

1. Every module lives in exactly one layer.
2. A module may import its own layer or a lower layer, never a higher one.
3. There are no import cycles.
4. ``trading_bot`` stays a leaf: only the documented entry points may import it.
5. Known oversized files may not grow (line-count ceiling).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# ---------------------------------------------------------------------------
# Layers. Lower number = more foundational. An import must point at the same
# layer or a lower one (higher index), never upwards.
# ---------------------------------------------------------------------------
LAYERS: dict[int, tuple[str, ...]] = {
    # Stable contracts and pure rule helpers: no src dependencies of their own.
    0: (
        "api_models",
        "hailo_contract",
        "strategy_rules",
    ),
    # Focused services: data access, indicators, persistence, prediction,
    # metrics and model training.
    1: (
        "ai_copilot_log",
        "ai_copilot_service",
        "analyze_trade_journal",
        "analytics",
        "backtesting",
        "bot_bootstrap",
        "bot_config",
        "coin_analysis",
        "data_preparation",
        "dry_run_execution",
        "entry_selection",
        "entry_filters",
        "env_file",
        "execution_service",
        "execution_owner",
        "bot_runtime",
        "inference_fallback",
        "news_veto",
        "exit_policy",
        "export_pnl_metrics",
        "exporter_paths",
        "get_account_balance",
        "get_data",
        "go_no_go_scorecard",
        "hailo_runtime",
        "indicator_service",
        "journal_guards",
        "market_analysis",
        "market_data",
        "market_observations",
        "open_trade_manager",
        "portfolio",
        "pnl_exporter",
        "pnl_metrics",
        "pnl_prometheus_format",
        "portfolio_bootstrap",
        "predict",
        "predict_catboost",
        "predict_hailo_remote",
        "recent_pnl_guard",
        "research_signal",
        "simulation",
        "state_store",
        "trade_journal",
        "train_catboost_model",
        "train_hailo_model",
        "train_trading_model",
    ),
    # Orchestration and process entry points.
    2: (
        "fastapi_app",
        "hailo_service",
        "historical_backtest",
        "init_kraken_paper",
        "public_market_recorder",
        "public_paper_dashboard",
        "trading_bot",
    ),
}

LAYER_OF: dict[str, int] = {
    name: layer for layer, names in LAYERS.items() for name in names
}

# Modules that are allowed to depend on the orchestrator. Keeping this list
# tiny is what stops the God class from becoming a dependency hub again.
TRADING_BOT_IMPORTERS: frozenset[str] = frozenset({"init_kraken_paper"})

# Line-count ceilings for files that are known to be too large. Lower these
# when a file shrinks; never raise them without an explicit decision.
LINE_CEILINGS: dict[str, int] = {
    "trading_bot.py": 2120,
    "entry_selection.py": 750,
    "public_paper_dashboard.py": 695,
    "export_pnl_metrics.py": 366,
}


def _src_modules() -> dict[str, Path]:
    return {
        path.stem: path
        for path in SRC.glob("*.py")
        if path.stem != "__init__" and not path.name.startswith("_")
    }


def _imports_of(module: str, path: Path) -> set[str]:
    """Return the ``src`` modules that ``module`` imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # Relative import inside the flat ``src`` package.
                if node.module:
                    found.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("src."):
                found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src."):
                    found.add(alias.name.split(".")[1])
    found.discard(module)
    return {name for name in found if name in _known_modules()}


_KNOWN: set[str] | None = None


def _known_modules() -> set[str]:
    global _KNOWN
    if _KNOWN is None:
        _KNOWN = set(_src_modules())
    return _KNOWN


def test_every_module_is_layered() -> None:
    """No module may be added without assigning it a layer."""
    unlayered = sorted(set(_src_modules()) - set(LAYER_OF))
    assert not unlayered, (
        "New src module(s) without a layer assignment: "
        f"{unlayered}. Add them to LAYERS in tests/test_architecture.py."
    )


def test_no_upward_imports() -> None:
    """A module may only import its own layer or a lower one."""
    violations: list[str] = []
    for name, path in sorted(_src_modules().items()):
        source_layer = LAYER_OF[name]
        for target in sorted(_imports_of(name, path)):
            target_layer = LAYER_OF[target]
            if target_layer > source_layer:
                violations.append(
                    f"{name} (layer {source_layer}) -> {target} "
                    f"(layer {target_layer})"
                )
    assert not violations, "Upward imports detected:\n" + "\n".join(violations)


def test_no_import_cycles() -> None:
    graph = {
        name: _imports_of(name, path)
        for name, path in _src_modules().items()
    }
    stack: list[str] = []
    on_stack: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def visit(node: str) -> None:
        if node in on_stack:
            cycles.append(" -> ".join(stack[stack.index(node):] + [node]))
            return
        if node in done:
            return
        on_stack.add(node)
        stack.append(node)
        for neighbour in sorted(graph.get(node, ())):
            visit(neighbour)
        stack.pop()
        on_stack.remove(node)
        done.add(node)

    for module in sorted(graph):
        visit(module)

    assert not cycles, "Import cycles detected:\n" + "\n".join(cycles)


def test_trading_bot_stays_a_leaf() -> None:
    """Only documented entry points may import the orchestrator."""
    importers = sorted(
        name
        for name, path in _src_modules().items()
        if "trading_bot" in _imports_of(name, path)
    )
    unexpected = [name for name in importers if name not in TRADING_BOT_IMPORTERS]
    assert not unexpected, (
        f"{unexpected} import(s) src.trading_bot. Orchestration must stay a "
        "leaf: depend on the focused services instead, or extend "
        "TRADING_BOT_IMPORTERS deliberately."
    )


def test_oversized_files_do_not_grow() -> None:
    for filename, ceiling in sorted(LINE_CEILINGS.items()):
        path = SRC / filename
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= ceiling, (
            f"src/{filename} grew to {lines} lines (ceiling {ceiling}). "
            "Extract the new code into a focused module instead."
        )

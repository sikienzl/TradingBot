"""Cyclomatic-complexity ratchet for the whole repository.

Clean Code asks for functions that a reader can hold in their head at once.
The repo's ruff gate (``lint.mccabe.max-complexity`` in ``pyproject.toml``)
enforces the limit for new code, and every hotspot that predates the limit
carries an explicit ``# noqa: C901``.

This module is the other half of that ratchet. Ruff alone cannot tell a
deliberate exception from an accidental one, so the exceptions are pinned here
by qualified name together with the complexity they are allowed to have:

* a new hotspot above the limit fails ruff (no ``noqa``) and this test
  (not pinned);
* a pinned hotspot that grows fails this test;
* a pinned hotspot that is split below the limit loses its ``# noqa: C901``
  because ruff's unused-noqa check (RUF100) flags the leftover marker, and this
  test then demands the stale entry is deleted.

The exception list can therefore only ever shrink.

The complexity numbers are computed with ``ast`` only, because the CI test job
does not install ruff. The visitor below reproduces ruff's McCabe counting
exactly: a function starts at 1 and gains 1 for each ``if``/``elif``, ``for``,
``async for``, ``while``, ``except`` handler, nested ``def``/``async def``
(whose body is counted into the enclosing function as well), ``match`` case and
``try``/``else`` clause. Boolean operators, conditional expressions,
comprehensions, ``with`` and ``assert`` do not add branches, and a ``lambda`` is
not a separate function.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Must equal ``[tool.ruff.lint.mccabe] max-complexity`` in pyproject.toml.
MAX_COMPLEXITY = 10

# Directories ruff does not scan (``[tool.ruff] exclude`` plus caches).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "catboost_info",
        "results",
    }
)

# Known hotspots: ``relative/path.py::qualified.name`` -> complexity allowed
# today. Lower a value when a hotspot is partly simplified and delete the entry
# once it reaches MAX_COMPLEXITY. Never add a value without also marking the
# function's def line with the matching C901 suppression comment.
EXCEPTIONS: dict[str, int] = {
    ".github/scripts/cluster_release.py::allowed_path": 11,
    ".github/scripts/cluster_release.py::check_content": 11,
    ".github/scripts/cluster_starter_release.py::build": 12,
    ".github/scripts/cluster_starter_release.py::check_content": 15,
    "cluster-starter/roles/storage/files/inspect_disk.py::inspect": 11,
    "scripts/fetch_research_signal.py::main": 13,
    "scripts/hailo_compile_onnx.py::main": 11,
    "scripts/public_paper_daily_report.py::log_report": 12,
    "scripts/recommend_tuning_change.py::recommend": 15,
    "scripts/run_tuning_cycle.py::main": 12,
    "scripts/run_tuning_cycle.py::_build_safety_guard_result": 13,
    "scripts/update_autoresearch_signal.py::_discover_repo_candidates": 11,
    "scripts/update_autoresearch_signal.py::main": 12,
    "src/ai_copilot_service.py::can_run_ai_copilot": 12,
    "src/analyze_trade_journal.py::summarize": 12,
    "src/backtesting.py::Backtester.run": 12,
    "src/coin_analysis.py::analyze_coin": 17,
    "src/entry_filters.py::passes_downtrend_reversal_filter": 16,
    "src/execution_service.py::confirm_order_fill_record": 16,
    "src/execution_service.py::execute_real_trade": 15,
    "src/export_pnl_metrics.py::read_ai_copilot_usage": 11,
    "src/fill_reconciliation.py::reconcile_journal_fills": 11,
    "src/go_no_go_scorecard.py::_evaluate_verdict": 16,
    "src/hailo_runtime.py::HailoModelMetadata.load": 12,
    "src/historical_backtest.py::ReplaySettings.validate": 13,
    "src/historical_backtest.py::load_ohlcv": 13,
    "src/journal_guards.py::_compute_degraded_entry_sources": 11,
    "src/journal_guards.py::_compute_dynamic_lossmaker_entry_pairs": 11,
    "src/market_analysis.py::dynamic_excluded_coins": 12,
    "src/market_analysis.py::select_analysis_coins": 11,
    "src/market_data.py::fetch_market_data": 11,
    "src/market_observations.py::_valid_rows": 12,
    "src/open_trade_manager.py::manage_open_trades": 12,
    "src/portfolio_bootstrap.py::refresh_live_portfolio_balance": 16,
    "src/public_market_recorder.py::PublicMarketRecorder._record_candles": 12,
    "src/public_paper_dashboard.py::_read_bot_log": 14,
    "src/public_paper_dashboard.py::load_dashboard_data": 14,
    "src/risk_state.py::parse_risk_state": 16,
    "src/train_hailo_model.py::prepare_hailo_data": 11,
    "src/train_hailo_model.py::train_and_export": 12,
    "tests/test_public_paper_transactions.py::test_restart_completes_interrupted_trade_once": 11,
}


class _BranchCounter(ast.NodeVisitor):
    """Counts the branch points ruff's McCabe check counts."""

    def __init__(self) -> None:
        self.complexity = 1

    def visit_If(self, node: ast.If) -> None:
        self.complexity += 1
        self.generic_visit(node)

    visit_AsyncFor = visit_If
    visit_For = visit_If
    visit_While = visit_If
    visit_ExceptHandler = visit_If

    def visit_Try(self, node: ast.Try) -> None:
        if node.orelse:
            self.complexity += 1
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A nested def is one extra exit path out of the enclosing function.
        self.complexity += 1
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Match(self, node: ast.Match) -> None:
        self.complexity += len(node.cases)
        self.generic_visit(node)


def _complexity_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    counter = _BranchCounter()
    for child in ast.iter_child_nodes(node):
        counter.visit(child)
    return counter.complexity


def _python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO.rglob("*.py"):
        if SKIP_DIRS.intersection(path.relative_to(REPO).parts):
            continue
        files.append(path)
    return sorted(files)


def _functions_in(path: Path) -> list[tuple[str, int, int]]:
    """Return ``(qualified name, line, complexity)`` for every def in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    class _Walker(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.rows: list[tuple[str, int, int]] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.scope.append(node.name)
            self.rows.append((".".join(self.scope), node.lineno, _complexity_of(node)))
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

    walker = _Walker()
    walker.visit(tree)
    return walker.rows


_ALL_FUNCTIONS: dict[str, tuple[str, int, int]] | None = None


def _all_functions() -> dict[str, tuple[str, int, int]]:
    """Map ``path::qualified name`` to ``(path, line, complexity)``.

    Cached: the repository is not rewritten while the tests run, and parsing
    every module once keeps the ratchet cheap enough to stay in the test job.
    """
    if _ALL_FUNCTIONS is not None:
        return _ALL_FUNCTIONS
    found: dict[str, tuple[str, int, int]] = {}
    for path in _python_files():
        rel = str(path.relative_to(REPO))
        for qual, line, complexity in _functions_in(path):
            key = f"{rel}::{qual}"
            assert key not in found, f"duplicate function key {key}"
            found[key] = (rel, line, complexity)
    globals()["_ALL_FUNCTIONS"] = found
    return found


def test_ratchet_threshold_matches_ruff_config() -> None:
    """The ratchet and the lint gate must be the same number."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    configured = config["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]
    assert configured == MAX_COMPLEXITY, (
        f"pyproject.toml allows complexity {configured} but "
        f"tests/test_complexity.py ratchets at {MAX_COMPLEXITY}"
    )


def test_every_hotspot_above_the_limit_is_pinned() -> None:
    """No function may exceed the limit without a pinned exception."""
    unpinned = [
        f"{rel}:{line} {key.split('::')[1]} (complexity {complexity})"
        for key, (rel, line, complexity) in sorted(_all_functions().items())
        if complexity > MAX_COMPLEXITY and key not in EXCEPTIONS
    ]
    assert not unpinned, (
        f"{len(unpinned)} function(s) exceed complexity {MAX_COMPLEXITY} without "
        "a pinned exception. Split them into smaller units; if a split is "
        "genuinely impossible, add the function to EXCEPTIONS here AND a "
        "'# noqa: C901' on its def line:\n" + "\n".join(unpinned)
    )


def test_pinned_hotspots_do_not_grow() -> None:
    """A pinned exception is a ceiling, not a licence to add branches."""
    functions = _all_functions()
    grown: list[str] = []
    for key, pinned in sorted(EXCEPTIONS.items()):
        entry = functions.get(key)
        assert entry is not None, (
            f"pinned exception {key} no longer exists - remove it from "
            "EXCEPTIONS (and its '# noqa: C901' if the function is gone)"
        )
        rel, line, complexity = entry
        if complexity > pinned:
            grown.append(
                f"{rel}:{line} {key.split('::')[1]} grew from {pinned} to "
                f"{complexity}"
            )
    assert not grown, "pinned hotspot(s) got more complex:\n" + "\n".join(grown)


def test_pinned_hotspots_are_still_above_the_limit() -> None:
    """A hotspot that is simple again must leave the exception list."""
    functions = _all_functions()
    stale = [
        f"{key} is now {functions[key][2]}, at or below {MAX_COMPLEXITY}"
        for key in sorted(EXCEPTIONS)
        if key in functions and functions[key][2] <= MAX_COMPLEXITY
    ]
    assert not stale, (
        "EXCEPTIONS entry(ies) no longer need an exception - delete them and "
        "their '# noqa: C901':\n" + "\n".join(stale)
    )


def test_pinned_hotspots_carry_a_noqa_marker() -> None:
    """The pin and the lint suppression must stay in sync."""
    missing: list[str] = []
    for key, _pinned in sorted(EXCEPTIONS.items()):
        rel, line, _complexity = _all_functions()[key]
        def_line = (REPO / rel).read_text(encoding="utf-8").splitlines()[line - 1]
        if "# noqa: C901" not in def_line:
            missing.append(f"{rel}:{line} {key.split('::')[1]}")
    assert not missing, (
        "pinned hotspot(s) without '# noqa: C901' on the def line "
        "(ruff would fail):\n" + "\n".join(missing)
    )


def test_exception_list_has_no_dead_entries() -> None:
    """Every exception names a function that exists exactly once."""
    functions = _all_functions()
    unknown = sorted(key for key in EXCEPTIONS if key not in functions)
    assert not unknown, f"EXCEPTIONS entry(ies) not found: {unknown}"

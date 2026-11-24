import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = REPO_ROOT / "scripts" / "start_sim_bot.sh"
STOP_SCRIPT = REPO_ROOT / "scripts" / "stop_sim_bot.sh"
REGIME_SCRIPT = REPO_ROOT / "scripts" / "compare_simulation_regimes.py"
BOT_COMPAT_SCRIPT = REPO_ROOT / "scripts" / "trading_bot.py"


def _run_script(
    script: Path, env: dict[str, str], *, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        cwd=script.resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _process_start_ticks(pid: int) -> str:
    stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    return stat_line.rsplit(")", 1)[1].split()[19]


def _simulation_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        ENV_FILE=str(tmp_path / "simulation.env"),
        LOG_DIR=str(tmp_path / "logs"),
        PID_FILE=str(tmp_path / "logs" / "sim_bot.pid"),
    )
    Path(env["ENV_FILE"]).write_text("", encoding="utf-8")
    return env


def _isolated_repo(tmp_path: Path, module: str) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "isolated-repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tools").mkdir()
    for script in (START_SCRIPT, STOP_SCRIPT):
        shutil.copy2(script, root / "scripts" / script.name)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "trading_bot.py").write_text(module, encoding="utf-8")
    (root / "tools" / "python").symlink_to(sys.executable)

    env = os.environ.copy()
    env.update(
        ENV_FILE=str(root / "simulation.env"),
        LOG_DIR=str(root / "logs"),
        PID_FILE=str(root / "logs" / "sim_bot.pid"),
        PYTHON_BIN="tools/python",
    )
    Path(env["ENV_FILE"]).write_text("", encoding="utf-8")
    return root, env


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_start_uses_module_and_reports_quick_module_failure(tmp_path: Path) -> None:
    root, env = _isolated_repo(tmp_path, "raise SystemExit(23)\n")

    result = _run_script(root / "scripts" / "start_sim_bot.sh", env)

    assert result.returncode != 0
    assert "failed to start (exit code 23)" in result.stderr
    assert "Simulation bot started." not in result.stdout
    assert not Path(env["PID_FILE"]).exists()
    assert Path(env["LOG_DIR"], "sim_bot.log").exists()


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_pid_file_creation_failure_never_starts_unmanaged_bot(tmp_path: Path) -> None:
    marker = tmp_path / "bot-started"
    root, env = _isolated_repo(
        tmp_path,
        "import os\nfrom pathlib import Path\nimport time\n"
        "Path(os.environ['BOT_MARKER']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    env["BOT_MARKER"] = str(marker)
    blocked_parent = root / "not-a-directory"
    blocked_parent.write_text("block", encoding="ascii")
    env["PID_FILE"] = str(blocked_parent / "sim_bot.pid")

    result = _run_script(root / "scripts" / "start_sim_bot.sh", env)

    assert result.returncode != 0
    assert "failed to start" in result.stderr
    assert "Could not create PID file" in Path(env["LOG_DIR"], "sim_bot.log").read_text()
    assert not marker.exists()
    assert not Path(env["PID_FILE"]).exists()


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_successful_start_and_stop_with_relative_python_in_isolated_repo(tmp_path: Path) -> None:
    marker = tmp_path / "bot-started"
    root, env = _isolated_repo(
        tmp_path,
        "import os\nfrom pathlib import Path\nimport time\n"
        "Path(os.environ['BOT_MARKER']).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    env["BOT_MARKER"] = str(marker)
    start_script = root / "scripts" / "start_sim_bot.sh"
    stop_script = root / "scripts" / "stop_sim_bot.sh"

    start_result = _run_script(start_script, env)
    assert start_result.returncode == 0, start_result.stderr
    assert "Simulation bot started." in start_result.stdout
    assert marker.exists()
    bot_pid = int(marker.read_text(encoding="ascii"))
    pid_record = Path(env["PID_FILE"]).read_text(encoding="ascii").splitlines()
    assert pid_record[0] == str(bot_pid)
    assert pid_record[2] == str(root / "tools" / "python")

    try:
        env.pop("PYTHON_BIN")
        stop_result = _run_script(stop_script, env)
        assert stop_result.returncode == 0, stop_result.stderr
        assert "Simulation bot stopped successfully." in stop_result.stdout
        assert not Path(env["PID_FILE"]).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(bot_pid, 0)
    finally:
        if Path(env["PID_FILE"]).exists():
            _run_script(stop_script, env)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_start_treats_reused_pid_as_stale_without_signaling_process(tmp_path: Path) -> None:
    env = _simulation_env(tmp_path)
    foreign_process = subprocess.Popen(["sleep", "30"])
    pid_file = Path(env["PID_FILE"])
    pid_file.parent.mkdir(parents=True)
    current_ticks = int(_process_start_ticks(foreign_process.pid))
    pid_file.write_text(
        f"{foreign_process.pid}\n{current_ticks + 1}\n{sys.executable}\n",
        encoding="ascii",
    )

    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env["PYTHON_BIN"] = str(fake_python)

    try:
        result = _run_script(START_SCRIPT, env)

        assert result.returncode != 0
        assert "failed to start" in result.stderr
        assert "already running" not in result.stdout
        assert foreign_process.poll() is None
        assert not pid_file.exists()
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_stop_refuses_to_signal_a_foreign_process_with_recorded_pid(tmp_path: Path) -> None:
    env = _simulation_env(tmp_path)
    foreign_process = subprocess.Popen(["sleep", "30"])
    pid_file = Path(env["PID_FILE"])
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(
        f"{foreign_process.pid}\n{_process_start_ticks(foreign_process.pid)}\n{sys.executable}\n",
        encoding="ascii",
    )

    try:
        result = _run_script(STOP_SCRIPT, env)

        assert result.returncode != 0
        assert "does not match" in result.stderr
        assert foreign_process.poll() is None
        assert pid_file.exists()
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_stop_removes_reused_pid_record_without_signaling_process(tmp_path: Path) -> None:
    env = _simulation_env(tmp_path)
    foreign_process = subprocess.Popen(["sleep", "30"])
    pid_file = Path(env["PID_FILE"])
    pid_file.parent.mkdir(parents=True)
    current_ticks = int(_process_start_ticks(foreign_process.pid))
    pid_file.write_text(
        f"{foreign_process.pid}\n{current_ticks + 1}\n{sys.executable}\n",
        encoding="ascii",
    )

    try:
        result = _run_script(STOP_SCRIPT, env)

        assert result.returncode == 0
        assert "without signaling it" in result.stdout
        assert foreign_process.poll() is None
        assert not pid_file.exists()
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)


@pytest.mark.skipif(not Path("/proc").exists(), reason="Linux /proc is required")
def test_stop_rejects_legacy_pid_file_without_signaling_process(tmp_path: Path) -> None:
    env = _simulation_env(tmp_path)
    foreign_process = subprocess.Popen(["sleep", "30"])
    pid_file = Path(env["PID_FILE"])
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(f"{foreign_process.pid}\n", encoding="ascii")

    try:
        result = _run_script(STOP_SCRIPT, env)

        assert result.returncode != 0
        assert "Invalid or legacy PID file" in result.stderr
        assert foreign_process.poll() is None
        assert pid_file.exists()
    finally:
        foreign_process.terminate()
        foreign_process.wait(timeout=5)


def test_regime_comparison_and_compatibility_wrappers_use_src_module() -> None:
    regime_tree = ast.parse(REGIME_SCRIPT.read_text(encoding="utf-8"))
    compat_tree = ast.parse(BOT_COMPAT_SCRIPT.read_text(encoding="utf-8"))

    assert any(
        isinstance(node, ast.ImportFrom) and node.module == "src.trading_bot"
        for node in regime_tree.body
    )
    assert sum(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        for node in regime_tree.body
    ) == 1
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "import_module"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "src.trading_bot"
        for node in ast.walk(compat_tree)
    )

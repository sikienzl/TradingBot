"""Job definitions to integrate autoresearch with existing schedulers."""
from .runner import AutoResearchRunner
from .storage import FileStorage


def run_example_job(out_path: str = "results/autoresearch_results.json"):
    storage = FileStorage(out_path)
    runner = AutoResearchRunner(storage)
    return runner.run_experiment("example", {"demo": True}, None)

"""Simple storage layer for autoresearch results."""
import json
from pathlib import Path


class FileStorage:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def save_result(self, result: dict):
        out = self.path
        if not out.exists():
            out.write_text("[]")
        raw = out.read_text()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = []
        # normalize to list
        if isinstance(data, dict):
            data = [data]
        data.append(result)
        out.write_text(json.dumps(data, indent=2))

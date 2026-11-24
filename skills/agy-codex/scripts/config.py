"""Read plugin-only JSON; never edit native Codex settings."""
from pathlib import Path
import os


def load(root):
    from controller import WorkflowError, read_json, codex_settings
    global_path = Path(os.environ.get("AGY_CODEX_CONFIG", str(
        Path.home()/".gemini"/"config"/"agy-codex"/"config.json"))).expanduser()
    result = {"codex": {}, "routing": {}}
    for path in (global_path, root/"config.json"):
        try:
            value = read_json(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise WorkflowError(f"Cannot read AGY Codex configuration {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise WorkflowError(f"Configuration must be a JSON object: {path}")
        result["codex"] = codex_settings(result["codex"], value.get("codex"))
        if "backend" in value:
            if value["backend"] not in ("auto", "app-server", "exec"):
                raise WorkflowError("backend must be auto, app-server, or exec.")
            result["backend"] = value["backend"]
        if "routing" in value:
            if not isinstance(value["routing"], dict):
                raise WorkflowError("routing must be an object.")
            result["routing"].update(value["routing"])
    return result

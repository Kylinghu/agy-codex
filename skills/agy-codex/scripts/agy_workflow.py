# Run inside Antigravity: /run-workflow Run <path>/agy_workflow.py with args {...}
# phase, log, agent, args and WORKSPACES are provided by the AGY workflow host.
# This file intentionally uses top-level await; it is not a standalone Python CLI.
from pathlib import Path
import sys

_options = dict(args or {})
if _options.get("controller_dir"):
    _controller_dir = Path(_options["controller_dir"]).expanduser().resolve()
elif globals().get("__file__"):
    _controller_dir = Path(__file__).resolve().parent
else:
    _controller_dir = None
if _controller_dir is None or not all((_controller_dir/name).is_file() for name in ("controller.py", "agy_adapter.py")):
    raise RuntimeError("Set args.controller_dir to the extracted directory containing controller.py and agy_adapter.py.")
sys.path.insert(0, str(_controller_dir))
from agy_adapter import run_workflow

await run_workflow(
    agent_fn=agent,
    phase_fn=phase,
    log_fn=log,
    workflow_args=_options,
    workspaces=globals().get("WORKSPACES", ()),
)

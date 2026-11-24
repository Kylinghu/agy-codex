"""Round-local observations derived from native thread cumulative token counters.

A resumed Thread may already have usage. The first current-round update's
total minus its last increment establishes the baseline; subsequent cumulative
snapshots replace the high-water mark, never sum repeated notifications.
"""
from controller import atomic_json, read_json

FIELDS = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
          "outputTokens": "output_tokens"}


def counters(value):
    if not isinstance(value, dict):
        return None
    result = {dest: value.get(source) for source, dest in FIELDS.items()}
    if any(type(n) is not int or n < 0 for n in result.values()):
        return None
    return result


class RoundUsage:
    def __init__(self, directory):
        self.path = directory/"round-usage.json"
        self.baseline = None
        self.latest = None
        self.data = {}

    def update(self, event):
        usage = event.get("tokenUsage")
        if not isinstance(usage, dict):
            return
        total = counters(usage.get("total"))
        if total is None:
            return
        if self.baseline is None:
            last = counters(usage.get("last"))
            if last is None or any(last[k] > total[k] for k in total):
                return
            self.baseline = {k: total[k] - last[k] for k in total}
        if self.latest is not None and any(total[k] < self.latest[k] for k in total):
            return
        if total == self.latest:
            return
        self.latest = total
        self.data = {k: total[k] - self.baseline[k] for k in total}
        atomic_json(self.path, {"baseline": self.baseline, "latest": self.latest,
                                "usage": self.data})


def observed(directory):
    path = directory/"round-usage.json"
    if not path.exists():
        return {}
    return read_json(path).get("usage", {})

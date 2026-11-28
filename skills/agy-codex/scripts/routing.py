"""Configured tier mapping, provider restrictions and read-only capability discovery."""
from config import load


def policy(root):
    from runtime import WorkflowError, codex_settings
    value = load(root)["routing"]
    if set(value) - {"strategy", "tiers", "default_tier", "allow_upgrade", "allowed_providers"}:
        raise WorkflowError("Unknown routing configuration field.")
    strategy = value.get("strategy", "auto")
    if strategy not in ("auto", "economy", "quality", "manual"):
        raise WorkflowError("Unknown routing strategy.")
    tiers = value.get("tiers", {})
    if not isinstance(tiers, dict):
        raise WorkflowError("routing.tiers must be an object.")
    tiers = {name: codex_settings(settings) for name, settings in tiers.items()}
    if any(not entry.get("model") for entry in tiers.values()):
        raise WorkflowError("Each tier needs an exact model ID.")
    allowed = value.get("allowed_providers", [])
    if not isinstance(allowed, list) or not all(isinstance(x, str) and x for x in allowed):
        raise WorkflowError("allowed_providers must be a list of provider IDs.")
    if "default_tier" in value:
        default = value["default_tier"]
        if not isinstance(default, str) or default not in tiers:
            raise WorkflowError(f"Unknown configured default_tier: {default}")
    else:
        implicit = {"economy": "economy", "quality": "heavy"}.get(strategy, "standard")
        default = implicit if implicit in tiers else None
    upgrade = value.get("allow_upgrade", True)
    if type(upgrade) is not bool:
        raise WorkflowError("allow_upgrade must be boolean.")
    return {"strategy": strategy, "tiers": tiers, "default_tier": default,
            "allow_upgrade": upgrade, "allowed_providers": allowed}


def select(root, tier=None):
    from runtime import WorkflowError
    p = policy(root)
    if tier is None:
        if p["strategy"] == "manual" or p["default_tier"] is None:
            return {}
        tier = p["default_tier"]
    if not isinstance(tier, str) or tier not in p["tiers"]:
        raise WorkflowError(f"Unknown configured Codex tier: {tier}")
    return dict(p["tiers"][tier])


def verify_provider(root, provider):
    from runtime import WorkflowError
    allowed = policy(root)["allowed_providers"]
    if allowed and provider not in allowed:
        raise WorkflowError("Codex provider is outside the configured allowlist.")


def validate_catalog(requested, models):
    from runtime import WorkflowError
    model = requested.get("model")
    if not model:
        return
    entry = next((m for m in models if model in (m.get("id"), m.get("model"))), None)
    if entry is None:
        raise WorkflowError(f"Requested Codex model is not in the selected catalog: {model}")
    effort = requested.get("reasoning_effort")
    choices = entry.get("supportedReasoningEfforts") or []
    supported = {x.get("reasoningEffort") if isinstance(x, dict) else x for x in choices}
    if effort and supported and effort not in supported:
        raise WorkflowError(f"Requested effort {effort} is not in the model catalog.")


def discover(rpc, state_dir, cwd, provider=None, *, include_rates=True):
    from runtime import WorkflowError
    from backends.codex_app_server import RpcError
    config = rpc.call("config/read", {"cwd": str(cwd), "includeLayers": False}, timeout=20).get("config", {})
    provider_response = rpc.call("modelProvider/list", {}, timeout=20)
    provider = provider or config.get("model_provider") or provider_response.get("defaultProviderId")
    providers = provider_response.get("data", [])
    catalog, seen = [], set()
    cursor = None
    while True:
        params = {"modelProvider": provider} if provider else {}
        if cursor:
            params["cursor"] = cursor
        page = rpc.call("model/list", params, timeout=20)
        catalog.extend(page.get("data", []))
        cursor = page.get("nextCursor")
        if not cursor:
            break
        if cursor in seen or len(seen) >= 20:
            raise WorkflowError("Model catalog pagination did not terminate.")
        seen.add(cursor)
    rates = None
    if include_rates:
        from runtime import Interrupted
        try:
            rates = rpc.call("account/rateLimits/read", {}, timeout=10)
        except (RpcError, Interrupted):
            if rpc.cancel.is_set():
                raise
    # Only expose noncredential fields, never native config layers or provider URLs.
    return {"default_model": config.get("model"), "default_provider": provider,
            "default_effort": config.get("model_reasoning_effort"),
            "providers": [x.get("id") if isinstance(x, dict) else x for x in providers],
            "models": [{k: m.get(k) for k in ("id", "model", "displayName",
                        "defaultReasoningEffort", "supportedReasoningEfforts", "isDefault")}
                       for m in catalog if isinstance(m, dict)],
            "rate_limits": None if rates is None else {
                "ordinaryUsageAllowed": rates.get("ordinaryUsageAllowed"),
                "rateLimits": {k: (rates.get("rateLimits") or {}).get(k)
                               for k in ("primary", "secondary", "planType")}},
            "catalog_is_not_entitlement": True, "routing": policy(state_dir)}


CATALOG_TTL_SECONDS = 600
RATES_TTL_SECONDS = 120


def _cache_key(state_dir, requested, include_rates, launcher):
    import hashlib
    from pathlib import Path
    from runtime import current_codex_home, encode
    codex_home = current_codex_home()
    cfg_toml = Path(codex_home) / "config.toml"
    cfg_stat = None
    try:
        st = cfg_toml.stat()
        cfg_stat = [st.st_mtime_ns, st.st_size]
    except OSError:
        cfg_stat = None
    raw = encode({
        "codex_home": codex_home,
        "codex_config_stat": cfg_stat,
        "launcher": list(launcher),
        "requested": requested,
        "routing": policy(state_dir),
        "include_rates": bool(include_rates),
    })
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def probe(target, *, codex_cmd=None, refresh=False, include_rates=True):
    """Discover models and rate limits for either a Workspace or a Strict Controller.

    Uses a lightweight TTL cache (invalidated on config/provider/CODEX_HOME change
    or explicit refresh=True). Cache is never treated as entitlement proof.
    """
    import tempfile
    import time
    from pathlib import Path
    from runtime import Workspace, atomic_json, read_json, resolve_codex
    from backends.codex_app_server import Client

    if isinstance(target, Workspace):
        cwd, state_dir, launcher = target.root_path, target.state_dir, codex_cmd or resolve_codex()
    else:
        cwd, state_dir, launcher = target.repo, target.root, codex_cmd or target.codex_cmd or resolve_codex()

    requested = load(state_dir)["codex"]
    cache_path = Path(state_dir) / "model-catalog-cache.json"
    key = _cache_key(state_dir, requested, include_rates, launcher)
    ttl = RATES_TTL_SECONDS if include_rates else CATALOG_TTL_SECONDS
    if not refresh and cache_path.is_file():
        try:
            cached = read_json(cache_path)
            if (cached.get("key") == key
                    and isinstance(cached.get("cached_at"), (int, float))
                    and (time.time() - cached["cached_at"]) < ttl
                    and isinstance(cached.get("data"), dict)):
                out = dict(cached["data"])
                out["from_cache"] = True
                return out
        except (OSError, ValueError):
            pass

    with tempfile.TemporaryDirectory(prefix="agy-model-catalog-") as directory:
        with Client(cwd, launcher, directory, requested=requested, timeout=60) as rpc:
            data = discover(rpc, state_dir, cwd, requested.get("model_provider"), include_rates=include_rates)
    try:
        atomic_json(cache_path, {"key": key, "cached_at": time.time(), "data": data})
    except OSError:
        pass
    data["from_cache"] = False
    return data

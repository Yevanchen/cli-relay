"""Production routing §1.7. No model selection from prose or CLI/global defaults."""
from __future__ import annotations

from typing import Any, Dict

from .errors import UsageError

TIERS = {"simple": ("codex", "gpt-6.1-terra", "high"),
         "complex": ("codex", "gpt-6.1-sol", "max"),
         "highest_quality": ("claude", "claude-opus-5-5", "max")}
RESOURCE_CLASSES = ("standard", "computer_use")
LOCAL_MAX_ACTIVE = 2
LOCAL_MAX_COMPUTER_USE = 1


def uses_cu_budget(payload: Dict[str, Any]) -> bool:
    # Unclassified historical provider work reserves the scarce budget conservatively.
    # This is an accounting precaution, never a classification inferred from its prompt.
    resource = payload.get("resource_class")
    return resource == "computer_use" or (payload.get("engine") != "fixture" and resource not in RESOURCE_CLASSES)


def validate_new(payload: Dict[str, Any]) -> None:
    """Only new work is validated. Known keys keep their original payload/hash and receipt."""
    engine = payload["engine"]
    if payload.get("resource_class", "standard") not in RESOURCE_CLASSES:
        raise UsageError("resource_class must be standard or computer_use; classify explicitly")
    if engine == "fixture":
        reason = payload.get("test_only_reason", "deterministic fixture engine; no provider calls")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 256 or not reason.isprintable():
            raise UsageError("test_only_reason must be 1-256 printable characters")
        return  # Explicit engine=fixture cannot invoke a provider, even in a production ledger.
    if "test_only_reason" in payload:
        raise UsageError("test-only exemptions are restricted to engine=fixture; production engines must use a tier")
    task_class = payload.get("task_class")
    if not isinstance(task_class, str) or task_class not in TIERS:
        raise UsageError("new production submits require --task-class simple|complex|highest_quality", tiers=TIERS)
    if "resource_class" not in payload:
        raise UsageError("new production submits require --resource-class standard|computer_use")
    expected = TIERS[task_class]
    options = payload.get("options") or {}
    actual = (engine, options.get("model"), options.get("effort"))
    if actual != expected:
        raise UsageError("engine/model/effort must match the explicit task class; no defaults or deprecated models "
                         "(including gpt-6-astra). Use the listed tier exactly",
                         expected=dict(zip(("engine", "model", "effort"), expected)), task_class=task_class)
    if payload["resource_class"] == "computer_use" and task_class != "complex":
        raise UsageError("computer_use requires task_class=complex, codex gpt-6.1-sol/max")
    if engine == "codex" and "sandbox" not in options:
        raise UsageError('new Codex submits require an explicit sandbox (normally sandbox="workspace-write")')
    if engine == "codex" and options.get("sandbox") not in ("read-only", "workspace-write", "danger-full-access"):
        raise UsageError("Codex sandbox must be read-only or workspace-write, or separately opted-in full access")
    if options.get("sandbox") == "danger-full-access" and options.get("allow_full_access") is not True:
        raise UsageError("full access requires a separate explicit task opt-in; computer_use grants no extra permissions")


def settings(engine: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    options = payload.get("options") or {}
    fixture = engine == "fixture"
    return {"effective_settings": {
        "engine": engine, "model": options.get("model"), "effort": options.get("effort"),
        "task_class": payload.get("task_class", "test" if fixture else None),
        "resource_class": payload.get("resource_class", "standard" if fixture else None),
        "source": "fixture" if fixture else "explicit_task_options",
        "verification": "no_provider" if fixture else "invocation_only",
        **({"test_only_reason": payload.get("test_only_reason", "deterministic fixture engine; no provider calls")}
           if fixture else {})}}


def warnings(engine: str, payload: Dict[str, Any]):
    if engine == "fixture":
        return []
    try:
        validate_new({**payload, "engine": engine})
    except UsageError:
        return [{"code": "production_settings_unverified", "action": "historical task preserved; new submits need "
                 "explicit task/resource class, supported model/effort and Codex sandbox"}]
    return []

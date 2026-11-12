"""Engine adapters for the official CLIs (and a deterministic fixture).

Each adapter: validates options, builds argv (never via a shell), checks the auth path
before spawning, and parses the CLI's own machine-readable output. Codex full access, Codex
workspace network access and extra Claude tools require an explicit task opt-in;
``assert_safe`` re-checks every argv before it is used.
"""

from __future__ import annotations

import fcntl
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import business_status, git_state, policy, procs
from .errors import UsageError

ENGINES = ("claude", "codex", "fixture")

# Variables that route a CLI to API-key or third-party billing. Codex subscription tasks
# remove them from the child environment; Claude still blocks. Only names are reported.
BILLING_ENV: Dict[str, tuple] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BEDROCK_BASE_URL",
               "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY"),
    "fixture": (),
}
UNSETTABLE_ENV = tuple(sorted({name for names in BILLING_ENV.values() for name in names}))
CODEX_BILLING_MODES = ("subscription", "api")

# Markers of a calling Claude Code session; they must not leak into a child CLI.
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PROJECT_DIR", "CLAUDE_PID")

_FORBIDDEN = {
    "claude": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--bare", "--continue",
               "-c", "--fork-session", "--setting-sources"},
    "codex": {"--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--last", "--full-auto",
              "--dangerously-bypass-hook-trust", "--yolo"},
    "fixture": set(),
}

_OPTION_TYPES: Dict[str, Dict[str, tuple]] = {
    "claude": {"model": (str,), "effort": (str,), "allowed_tools": (list,),
               "allowed_write_dirs": (list,), "allowed_bash_commands": (list,), "require_completion_marker": (bool,)},
    "codex": {"model": (str,), "effort": (str,), "sandbox": (str,), "allow_full_access": (bool,),
              "allow_network": (bool,), "network_access": (bool,),
              "skip_git_repo_check": (bool,), "require_completion_marker": (bool,), "billing_mode": (str,)},
    "fixture": {"duration_s": (int, float), "exit_code": (int,), "result": (str,), "side_effect_file": (str,),
                "no_result": (bool,), "api_key_source": (str,), "permission_mode": (str,),
                "descendant_s": (int, float), "descendant_pid_file": (str,), "require_completion_marker": (bool,)},
}
for _spec in _OPTION_TYPES.values():
    _spec.update({"result_file": (str,), "require_business_status": (bool,), "required_artifacts": (list,)})

CODEX_SANDBOXES = ("read-only", "workspace-write")
CODEX_FULL_ACCESS = "danger-full-access"
CODEX_WORKSPACE_WRITE = "workspace-write"
# Network access inside workspace-write, loopback included: without it Seatbelt refuses even a bind
# to 127.0.0.1 (Codex 0.163.0, `[sandbox_workspace_write] network_access`). Every workspace-write run
# gets it explicitly, true only for a task with allow_network=true, so no config layer decides it.
CODEX_NETWORK_SETTING = "sandbox_workspace_write.network_access"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# With require_completion_marker=true (any engine) a run only ends succeeded when the last non-empty
# line of the CLI's final message is exactly COMPLETION_LINE_PREFIX + "complete". The relay reads
# that one line of the CLI's own result, never any other text of it.
COMPLETION_LINE_PREFIX = "CLI-RELAY-STATUS: "
COMPLETION_MARKERS = ("complete", "incomplete")
# One adapter contract: preflight, argv and runtime verification must agree. This only tightens
# the session default; existing allow/ask/deny rules and managed settings remain loaded.
CLAUDE_PERMISSION_MODE = "dontAsk"
CLAUDE_PERMISSION_MODES = ("default", "manual", "acceptEdits", "auto", "bypassPermissions", "dontAsk", "plan")
CLAUDE_EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
# Tools a Claude task may add with ``allowed_tools``: exact names only, never a rule pattern, a
# wildcard or another tool. Task/Agent remain excluded: a custom subagent can override dontAsk
# with permissionMode: acceptEdits/auto. See docs/claude-subagents.md before widening this list.
CLAUDE_NETWORK_TOOLS = ("WebFetch", "WebSearch")
CLAUDE_ALLOWABLE_TOOLS = ("TodoWrite", *CLAUDE_NETWORK_TOOLS)
CLAUDE_ALLOWED_TOOLS_FLAGS = ("--allowedTools", "--allowed-tools")
# Observed with Claude Code 2.1.312 under dontAsk, user settings excluded (issue #22): these commands,
# called by their bare name with any flag, are denied under every scoped rule (Bash(cp -n a b:*), the
# exact Bash(cp -n a b), Bash(cp:*), Bash(cp *)); only an unrestricted Bash rule allows them.
CLAUDE_BASH_FLAG_RULE_COMMANDS = {"cp", "mv"}
# Observed likewise: these commands, called by their bare name, are denied whenever a path argument
# resolves outside the working directory, even under a matching rule (interpreters such as python3
# are not checked this way).
CLAUDE_BASH_PATH_CHECKED_COMMANDS = {"cat", "cp", "ls", "mkdir", "mv", "rm", "touch"}

# Configuration layers outside the user's own files that the CLIs also read.
CODEX_SYSTEM_CONFIG = (Path("/etc/codex/config.toml"),)
CODEX_MANAGED_CONFIG = (Path("/etc/codex/managed_config.toml"),)
# Layers that can constrain or replace permissions in ways the legacy --sandbox flag does not
# override; cli-relay cannot evaluate them, so their presence blocks a Codex run.
CODEX_REQUIREMENTS = (Path("/etc/codex/requirements.toml"),
                      Path("/Library/Managed Preferences/com.openai.codex.plist"))
CLAUDE_MANAGED_SETTINGS = (Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
                           Path("/etc/claude-code/managed-settings.json"))
# Claude settings keys that obtain credentials other than the claude.ai login.
CLAUDE_CREDENTIAL_SETTINGS = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport")


@dataclass
class Preflight:
    ok: bool
    reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Spec:
    argv: List[str]
    env: Dict[str, str]
    cwd: str
    last_message_path: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)


def validate_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise UsageError(f"unknown engine {engine!r}", allowed=list(ENGINES))
    return engine


def _write_dirs(values: list, cwd: Optional[str]) -> List[str]:
    """Accept literal directory names, optionally ending in /**; never caller-supplied globs."""
    if values and cwd is None:
        raise UsageError("allowed_write_dirs requires the task cwd")
    root = Path(cwd).resolve() if cwd is not None else None
    result = []
    for value in values:
        if not isinstance(value, str):
            raise UsageError("allowed_write_dirs must contain directory strings")
        directory = value[:-3] if value.endswith("/**") else value
        if (not directory or not re.fullmatch(r"[A-Za-z0-9_./ -]+", directory)
                or ".." in directory.split("/") or directory != directory.strip()):
            raise UsageError("allowed_write_dirs requires literal directories without .. or rule syntax")
        path = Path(directory)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved_relative = resolved.relative_to(root)
            try:
                relative = path.relative_to(root)
            except ValueError:
                # The absolute spelling may use an OS alias such as /var -> /private/var.
                # Store a cwd-relative rule, never that outside lexical spelling.
                relative = resolved_relative
            if relative == Path(".") or resolved == root:
                raise ValueError("the whole cwd is not a subdirectory")
            if path.exists() and not path.is_dir():
                raise ValueError("not a directory")
            # A recursive grant must not hide an existing symlink escape beneath it.
            def unreadable(exc):
                raise exc

            children = os.walk(path, followlinks=False, onerror=unreadable) if path.exists() else ()
            for parent, dirs, files in children:
                for name in dirs + files:
                    child = Path(parent) / name
                    if child.is_symlink():
                        child.resolve().relative_to(resolved)
        except (ValueError, OSError, RuntimeError) as exc:
            raise UsageError("allowed_write_dirs must stay inside a task cwd subdirectory, including symlinks") from exc
        result.append("./" + relative.as_posix() + "/**")
    return sorted(set(result))


def _bash_commands(values: list) -> List[str]:
    result = []
    for value in values:
        # Prefix rules, not shell programs or raw Claude permission expressions. In particular
        # commas, parentheses, wildcards, quoting, substitutions and separators are forbidden.
        if (not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_./+-]+(?: [A-Za-z0-9_./+=-]+)*", value)
                or value.startswith("-") or "bypasspermissions" in value.lower()
                or "dangerously-skip-permissions" in value.lower()):
            raise UsageError("allowed_bash_commands requires literal command prefixes without shell or rule syntax")
        result.append(value)
    return sorted(set(result))


def validate_unset_env(values: Any) -> List[str]:
    if not isinstance(values, list):
        raise UsageError("unset_env must be a JSON list of environment variable names")
    if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in values):
        raise UsageError("unset_env names must match [A-Za-z_][A-Za-z0-9_]*; values are not accepted")
    if not set(values).issubset(UNSETTABLE_ENV):
        raise UsageError("unset_env accepts only billing environment variable names", allowed=list(UNSETTABLE_ENV))
    return sorted(set(values))


def validate_options(engine: str, options: Dict[str, Any], *, cwd: Optional[str] = None) -> Dict[str, Any]:
    spec = {**_OPTION_TYPES[engine], "unset_env": (list,)}
    clean: Dict[str, Any] = {}
    for key, value in sorted(options.items()):
        if key not in spec:
            raise UsageError(f"option {key!r} is not supported for engine {engine}", allowed=sorted(spec))
        types = spec[key]
        if isinstance(value, bool) and bool not in types:
            raise UsageError(f"option {key!r} has the wrong type")
        if not isinstance(value, types):
            raise UsageError(f"option {key!r} has the wrong type")
        clean[key] = value
    if "unset_env" in clean:
        clean["unset_env"] = validate_unset_env(clean["unset_env"])
    if engine == "codex" and "network_access" in clean:
        if "allow_network" in clean and clean["allow_network"] != clean["network_access"]:
            raise UsageError("network_access and allow_network must agree")
        clean["allow_network"] = clean.pop("network_access")
    if engine == "codex":
        if "billing_mode" in clean and clean["billing_mode"] not in CODEX_BILLING_MODES:
            raise UsageError("codex billing_mode must be subscription or api", allowed=list(CODEX_BILLING_MODES))
        sandboxes = (*CODEX_SANDBOXES, CODEX_FULL_ACCESS)
        if "sandbox" in clean and clean["sandbox"] not in sandboxes:
            raise UsageError("codex sandbox must be read-only, workspace-write or danger-full-access "
                             "(danger-full-access requires --option allow_full_access=true)",
                             allowed=list(sandboxes))
        if clean.get("sandbox") == CODEX_FULL_ACCESS and clean.get("allow_full_access") is not True:
            raise UsageError("codex sandbox danger-full-access requires --option allow_full_access=true; "
                             "full access disables the Codex sandbox and is off by default")
        if "allow_network" in clean and clean.get("sandbox") != CODEX_WORKSPACE_WRITE:
            if clean.get("sandbox") == CODEX_FULL_ACCESS:
                raise UsageError('allow_network is redundant with sandbox danger-full-access: full access already '
                                 'runs commands without the Codex sandbox, network included; drop allow_network '
                                 '(or use sandbox="workspace-write")')
            raise UsageError('allow_network applies only to sandbox="workspace-write" (add --option '
                             'sandbox="workspace-write"); the read-only sandbox, the default, has no network '
                             'access and allow_network does not change that', allowed=[CODEX_WORKSPACE_WRITE])
    if engine == "claude" and "allowed_tools" in clean:
        tools = clean["allowed_tools"]
        if not all(isinstance(tool, str) and tool in CLAUDE_ALLOWABLE_TOOLS for tool in tools):
            subagents = any(isinstance(tool, str) and tool in ("Task", "Agent") for tool in tools)
            reason = ("; Task/Agent opt-ins are not enabled because custom subagent permissionMode can "
                      "override dontAsk; see docs/claude-subagents.md" if subagents else "")
            raise UsageError("allowed_tools accepts only the exact tool names " + ", ".join(CLAUDE_ALLOWABLE_TOOLS) +
                             "; rule patterns, wildcards and other tools are not accepted" + reason,
                             allowed=list(CLAUDE_ALLOWABLE_TOOLS))
        if len(set(tools)) != len(tools):
            raise UsageError("allowed_tools names a tool more than once")
        clean["allowed_tools"] = sorted(tools)  # a set of tools: the order is not part of the request
    if engine == "claude":
        if "bypasspermissions" in json.dumps(clean).lower():
            raise UsageError("bypassPermissions is forbidden")
        if "allowed_write_dirs" in clean:
            clean["allowed_write_dirs"] = _write_dirs(clean["allowed_write_dirs"], cwd)
        if "allowed_bash_commands" in clean:
            clean["allowed_bash_commands"] = _bash_commands(clean["allowed_bash_commands"])
    if "effort" in clean and clean["effort"] not in EFFORT_LEVELS:
        raise UsageError("effort must be low, medium, high, xhigh or max", allowed=list(EFFORT_LEVELS))
    for key in ("side_effect_file", "descendant_pid_file"):
        if engine == "fixture" and key in clean and not os.path.isabs(clean[key]):
            raise UsageError(f"{key} must be an absolute path")
    if "result_file" in clean:
        clean["result_file"] = business_status.validate_result_file(clean["result_file"], cwd)
    if "required_artifacts" in clean:
        if cwd is None:
            raise UsageError("required_artifacts requires a task cwd")
        business_status.artifact_paths(clean["required_artifacts"], cwd)
    return clean


def resolve_bin(engine: str, env: Dict[str, str]) -> Optional[str]:
    override = env.get({"claude": "CLI_RELAY_CLAUDE_BIN", "codex": "CLI_RELAY_CODEX_BIN"}[engine])
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which(engine, path=env.get("PATH"))


def child_env(env: Dict[str, str], *, unset_env: Iterable[str] = ()) -> Dict[str, str]:
    return {k: v for k, v in env.items()
            if k not in unset_env and k not in SCRUB_ENV and not k.startswith("CLI_RELAY_TEST_")}


def billing_policy(engine: str, options: Dict[str, Any]) -> Dict[str, str]:
    """An omitted persisted mode always means subscription, including on legacy resumes."""
    if engine != "codex":
        return {}
    mode = options.get("billing_mode", "subscription")
    return {"billing_mode": mode,
            "billing_env_policy": "explicit_api" if mode == "api" else "subscription_isolation"}


def task_unset_env(engine: str, options: Dict[str, Any]) -> List[str]:
    names = set(validate_unset_env(options.get("unset_env", [])))
    if billing_policy(engine, options).get("billing_mode") == "subscription":
        names.update(BILLING_ENV[engine])
    return sorted(names)


def billing_env_evidence(engine: str, env: Dict[str, str], options: Dict[str, Any]) -> Dict[str, Any]:
    """Names-only snapshot before task cleanup; empty strings count as present."""
    policy = billing_policy(engine, options)
    if not policy:
        return {}
    present = sorted(name for name in BILLING_ENV[engine] if name in env)
    removed = task_unset_env(engine, options)
    return {**policy, "billing_env_present": present,
            "billing_env_removed": [name for name in present if name in removed],
            "billing_env_preserved": [name for name in present if name not in removed]}


def _first_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ------------------------------------------------------------------- provider configuration
def _home(env: Dict[str, str]) -> Path:
    return Path(env["HOME"]) if env.get("HOME") else Path.home()


def codex_home(env: Dict[str, str]) -> Path:
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else _home(env) / ".codex"


def claude_config_dir(env: Dict[str, str]) -> Path:
    return Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else _home(env) / ".claude"


def codex_system_requirements_paths(env: Dict[str, str]) -> List[Path]:
    user_name = env.get("USER") or os.environ.get("USER") or ""
    return [*CODEX_REQUIREMENTS,
            Path("/Library/Managed Preferences") / user_name / "com.openai.codex.plist"]


def _dirs_up(cwd: Optional[str]) -> List[Path]:
    """The task directory and all of its parents, outermost first."""
    if not cwd:
        return []
    start = Path(os.path.abspath(cwd))
    return [*reversed(start.parents), start]


def _existing_once(layers: Iterable[Tuple[str, Path]]) -> List[Tuple[str, Path]]:
    seen, out = set(), []
    for layer, path in layers:
        # Aliases may cross trust layers. A user symlink to a managed file must not erase
        # the managed provenance (or make its policy eligible for a user-only override).
        identity = (layer, os.path.realpath(path))
        if identity not in seen and os.path.isfile(path):
            seen.add(identity)
            out.append((layer, path))
    return out


def _merge(base: Dict[str, Any], top: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def _merged(layers: Iterable[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for _, data in layers:
        merged = _merge(merged, data)
    return merged


def _codex_layers(env: Dict[str, str], cwd: Optional[str]
                  ) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str], Optional[Path]]:
    """The parsed Codex configuration layers for ``cwd``, lowest precedence first (system, user, the
    config-profile file the configuration selects, every ``.codex/config.toml`` from the filesystem
    root down to the task directory, managed), the layers that cannot be read, and the profile file."""
    home = codex_home(env)

    def load(profile_file: Optional[Path]) -> Tuple[List[Tuple[str, Dict[str, Any]]], List[str]]:
        paths = [("system", p) for p in CODEX_SYSTEM_CONFIG] + [("user", home / "config.toml")]
        if profile_file is not None:  # Codex 0.163: `profile = X` layers $CODEX_HOME/X.config.toml on the user config
            paths.append(("profile_file", profile_file))
        paths += [("project", d / ".codex" / "config.toml") for d in _dirs_up(cwd)]
        paths += [("managed", p) for p in CODEX_MANAGED_CONFIG]
        layers: List[Tuple[str, Dict[str, Any]]] = []
        bad: List[str] = []
        for layer, path in _existing_once(paths):
            try:
                layers.append((layer, tomllib.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                bad.append(f"{layer}:unreadable")
        return layers, bad

    layers, bad = load(None)
    profile = _merged(layers).get("profile")
    if isinstance(profile, str) and (home / f"{profile}.config.toml").is_file():
        profile_file = home / f"{profile}.config.toml"
        return (*load(profile_file), profile_file)
    return layers, bad, None


def codex_config_problems(env: Dict[str, str], cwd: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Merge the Codex configuration layers (system, user, every ``.codex/config.toml`` from the
    filesystem root down to the task directory, managed) and name every setting that could serve the
    run from anything but the built-in OpenAI provider with the ChatGPT sign-in. Project layers are
    read whether or not Codex trusts the project, and an unreadable layer is a problem: when the
    configuration cannot be confirmed, the run is blocked. Only key names are reported."""
    home = codex_home(env)
    problems: List[str] = []
    if any(p.exists() for p in [*codex_system_requirements_paths(env), home / "requirements.toml"]):
        problems.append("requirements")
    layers, bad, profile_file = _codex_layers(env, cwd)
    merged, read = _merged(layers), [layer for layer, _ in layers]
    problems += bad
    # Every value is type-checked: a structure this check does not expect is itself a problem, so
    # an unusual file blocks the run instead of making the check fail (independent audit 6).
    tables: List[Tuple[str, Dict[str, Any]]] = [("", merged)]
    profiles = merged.get("profiles", {})
    if not isinstance(profiles, dict):
        problems.append("profiles")
        profiles = {}
    if "profile" in merged:
        profile = profiles.get(merged["profile"]) if isinstance(merged["profile"], str) else None
        if isinstance(profile, dict):
            tables.append(("profiles.<active>.", profile))
        elif profile_file is None:
            problems.append("profile")
    provider: Any = "openai"
    for prefix, table in tables:
        # Named permission profiles and permission overrides can take precedence over --sandbox
        # (independent audit 2), so any permission setting blocks the run rather than being trusted.
        problems += [prefix + key for key in sorted(table) if "permission" in key.lower()]
        provider = table.get("model_provider", provider)
        problems += [prefix + key for key in sorted(table) if key.endswith("base_url")]
        for key in ("forced_login_method", "preferred_auth_method"):
            if table.get(key) not in (None, "chatgpt"):
                problems.append(prefix + key)
    if provider != "openai":
        problems.append("model_provider")
    providers = merged.get("model_providers", {})
    if not isinstance(providers, dict):
        problems.append("model_providers")
    elif "openai" in providers:
        problems.append("model_providers.openai")
    return problems, read


def codex_workspace_network(env: Dict[str, str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """For doctor: what the checked Codex configuration layers set for network access in the
    workspace-write sandbox. A relay task never takes it from there (every workspace-write run gets
    the task's own value, off unless allow_network=true); this is what a direct ``codex`` run in
    ``cwd`` would get. Only layer names and booleans are reported, never other settings content."""
    layers, bad, _ = _codex_layers(env, cwd)
    setting, complete = set(), not bad
    for layer, data in layers:
        table = data.get("sandbox_workspace_write")
        if table is None:
            continue
        if not isinstance(table, dict) or not isinstance(table.get("network_access", False), bool):
            complete = False
        elif "network_access" in table:
            setting.add(layer)
    table = _merged(layers).get("sandbox_workspace_write")
    value = table.get("network_access") if isinstance(table, dict) else None
    return {"config_network_access": value if isinstance(value, bool) else None, "set_by_layers": sorted(setting),
            "complete": complete}


def _claude_layers(env: Dict[str, str], cwd: Optional[str]) -> List[Tuple[str, Path]]:
    """The Claude settings files that exist for ``cwd``: user, the project files of every directory
    from the filesystem root down to the task directory, and managed."""
    layers = [("user", claude_config_dir(env) / "settings.json")]
    for d in _dirs_up(cwd):
        layers += [("project", d / ".claude" / "settings.json"), ("project", d / ".claude" / "settings.local.json")]
    layers += [("managed", p) for p in CLAUDE_MANAGED_SETTINGS]
    return _existing_once(layers)


def claude_settings_problems(env: Dict[str, str], cwd: Optional[str] = None, *,
                             permission_mode: Optional[str] = None,
                             effort: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Check subscription routing and permission/effort constraints in the Claude settings layers.
    ``permission_mode`` is the adapter's known CLI override, not permission to skip settings.
    Without it a bypass default is unsafe. Only key and variable names are reported."""
    problems: List[str] = []
    read: List[str] = []
    for layer, path in _claude_layers(env, cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            problems.append(f"{layer}:unreadable")
            continue
        read.append(layer)
        problems += [f"{layer}:{key}" for key in CLAUDE_CREDENTIAL_SETTINGS if data.get(key)]
        if data.get("forceLoginMethod") not in (None, "claudeai"):
            problems.append(f"{layer}:forceLoginMethod")
        # A section of an unexpected type cannot be checked, so it is a problem itself.
        settings_env = data.get("env", {})
        if not isinstance(settings_env, dict):
            problems.append(f"{layer}:env")
            settings_env = {}
        problems += [f"{layer}:env.{name}" for name in BILLING_ENV["claude"] if settings_env.get(name)]
        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            problems.append(f"{layer}:permissions")
            permissions = {}
        mode = permissions.get("defaultMode")
        # The adapter pins dontAsk on every first/resumed run. Only that known CLI override
        # permits a non-managed bypass default; malformed or managed policy still blocks.
        # Task options can never request bypass (validate_options/assert_safe reject it).
        if ((mode is not None and mode not in CLAUDE_PERMISSION_MODES) or
                (mode == "bypassPermissions" and permission_mode != CLAUDE_PERMISSION_MODE) or
                (permission_mode == CLAUDE_PERMISSION_MODE and
                 (mode == "plan" or (layer == "managed" and mode not in (None, CLAUDE_PERMISSION_MODE))))):
            problems.append(f"{layer}:permissions.defaultMode")
        if effort and layer == "managed":
            managed_effort = settings_env.get(CLAUDE_EFFORT_ENV)
            if managed_effort is not None and managed_effort != effort:
                problems.append(f"{layer}:env.{CLAUDE_EFFORT_ENV}")
        if effort:
            # maxEffortLevel is restrictive across every scope: even a higher managed value
            # cannot lift a lower user/project cap. Do not spend a request at a known lower effort.
            cap = data.get("maxEffortLevel")
            if cap is not None and (cap not in EFFORT_LEVELS or EFFORT_LEVELS.index(cap) < EFFORT_LEVELS.index(effort)):
                problems.append(f"{layer}:maxEffortLevel")
    return problems, read


def claude_tool_rules(env: Dict[str, str], cwd: Optional[str] = None) -> Dict[str, Any]:
    """For doctor: what the checked Claude settings layers say about the network tools before any
    task option. Claude evaluates deny, then ask, then allow, and dontAsk denies an ask, so a deny
    or ask rule naming a tool (with or without a specifier, ``*`` matching in the tool name) still
    wins over ``allowed_tools``; only a bare allow rule allows a whole tool. Managed
    ``allowManagedPermissionRulesOnly`` drops every other source of rules, ``--allowedTools``
    included. Only the allowable tool names and booleans are reported, never settings content."""
    layers: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    complete = True
    for layer, path in _claude_layers(env, cwd):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        permissions = data.get("permissions", {}) if isinstance(data, dict) else None
        if not isinstance(permissions, dict):
            complete = False
            continue
        layers.append((layer, data, permissions))
    # Only managed settings can set it; a value that cannot be read as false is the restrictive one.
    flags = [data.get("allowManagedPermissionRulesOnly") for layer, data, _ in layers if layer == "managed"]
    managed_only = any(flag is not None and flag is not False for flag in flags)
    found: Dict[str, set] = {"allow": set(), "ask": set(), "deny": set()}
    for layer, _, permissions in layers:
        if managed_only and layer != "managed":
            continue
        for kind, names in found.items():
            rules = permissions.get(kind, [])
            if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
                complete = False
                rules = [rule for rule in rules if isinstance(rule, str)] if isinstance(rules, list) else []
            for tool in CLAUDE_NETWORK_TOOLS:
                if (tool in rules if kind == "allow" else
                        any(fnmatch.fnmatchcase(tool, rule.split("(", 1)[0].strip()) for rule in rules)):
                    names.add(tool)
    allowed = [tool for tool in CLAUDE_NETWORK_TOOLS if tool in found["allow"] - found["ask"] - found["deny"]]
    return {"allowed_by_default": allowed == list(CLAUDE_NETWORK_TOOLS), "default_allowed": allowed,
            "ask": sorted(found["ask"]), "denied": sorted(found["deny"]), "managed_rules_only": managed_only,
            "complete": complete}


def preflight(engine: str, env: Dict[str, str], cwd: Optional[str] = None, *,
              options: Optional[Dict[str, Any]] = None) -> Preflight:
    """Everything that decides which account serves the run is checked before the CLI starts:
    billing variables, the CLI's configuration layers that apply to ``cwd``, and the login."""
    opts = options or {}
    if engine == "codex":
        opts = validate_options(engine, opts, cwd=cwd)
    billing = billing_env_evidence(engine, env, opts)
    env = child_env(env, unset_env=task_unset_env(engine, opts))
    api = billing.get("billing_mode") == "api"

    def result(ok: bool, reason: Optional[str] = None, detail: Optional[Dict[str, Any]] = None) -> Preflight:
        return Preflight(ok, reason, {**billing, **(detail or {})})

    present = [name for name in BILLING_ENV[engine] if env.get(name)] if not api else []
    if present:
        flags = " ".join(f"--unset-env {name}" for name in present)
        prefix = "env " + " ".join(f"-u {name}" for name in present)
        return result(False, "billing_env_present", {
            "names": present, "fix": {"submit_flags": flags, "env_prefix": prefix},
            "message": f"Billing environment variables block subscription login. Submit with a new key and "
                       f"{flags}, or prefix the cli-relay command with {prefix}.",
        })
    if engine == "fixture":
        return result(True)
    if engine == "codex":
        problems, layers = codex_config_problems(env, cwd)
    else:
        opts = validate_options(engine, opts, cwd=cwd)
        problems, layers = claude_settings_problems(env, cwd, permission_mode=CLAUDE_PERMISSION_MODE,
                                                   effort=opts.get("effort"))
    if problems:
        return result(False, "config_not_subscription", {"settings": problems})
    binary = resolve_bin(engine, env)
    if not binary:
        return result(False, "cli_not_found", {"engine": engine})
    argv = [binary, "auth", "status"] if engine == "claude" else [binary, "login", "status"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=child_env(env),
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return result(False, "auth_unverified", {"error": type(exc).__name__})
    if engine == "claude":
        info = _first_json_object(out.stdout) or {}
        auth = {k: info.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        if (info.get("loggedIn") is True and info.get("authMethod") == "claude.ai"
                and info.get("apiProvider") in (None, "firstParty")):
            return result(True, None, {"auth": auth, "config_layers": layers})
        return result(False, "auth_not_subscription", {"auth": auth, "returncode": out.returncode})
    text = (out.stdout + "\n" + out.stderr).lower()
    if "api key" in text:
        if api and out.returncode == 0:
            return result(True, None, {"auth": "api_key", "config_layers": layers})
        return result(False, "auth_not_subscription", {"auth": "api_key"})
    if out.returncode == 0 and "chatgpt" in text:
        return result(True, None, {"auth": "chatgpt", "config_layers": layers})
    return result(False, "auth_unverified", {"auth": "unknown", "returncode": out.returncode})


def codex_rollout(env: Dict[str, str], thread_id: str) -> Optional[Path]:
    matches = glob.glob(str(codex_home(env) / "sessions" / "*" / "*" / "*" / f"rollout-*{glob.escape(thread_id)}.jsonl"))
    return Path(sorted(matches)[0]) if matches else None


# ------------------------------------------------------------------- existing sessions (issue #18)
# `submit --session ID` delivers the prompt as the next turn of an existing native session. The id is
# an exact UUID (a Claude session id or a Codex thread id), never a name, `--last` or a pattern.
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SESSION_PREFLIGHT_REASONS = ("session_not_found", "session_busy", "provider_not_subscription", "provider_unverified")
# Codex refuses a second writer on a thread with this message on stderr (observed with codex-cli 0.162.1).
CODEX_ACTIVE_WRITER = b"already has an active writer"


def validate_session_id(value: Any) -> str:
    sid = value.strip().lower() if isinstance(value, str) else ""
    if not SESSION_ID_RE.fullmatch(sid):
        raise UsageError("--session must be an exact native session UUID (a Claude session id or a Codex thread "
                         "id); names, --last and patterns are not accepted")
    return sid


def session_record(engine: str, env: Dict[str, str], session_id: str, *, fixture_state_dir: str) -> Optional[Path]:
    """The CLI's own record of the session on this machine, or None."""
    if engine == "claude":
        pattern = str(claude_config_dir(env) / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
        matches = sorted(glob.glob(pattern))
        return Path(matches[0]) if matches else None
    if engine == "codex":
        return codex_rollout(env, session_id)
    path = Path(fixture_state_dir) / f"{session_id}.json"
    return path if path.is_file() else None


def session_cwd(engine: str, record: Path) -> Optional[str]:
    """The directory the session recorded when it was created, if its first lines name one."""
    for n, ev in enumerate(iter_json_lines(record)):
        if n >= 50:
            return None
        payload = ev.get("payload") if engine == "codex" and ev.get("type") == "session_meta" else None
        cwd = payload.get("cwd") if isinstance(payload, dict) else ev.get("cwd") if engine == "claude" else None
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def _claude_registration_live(pid: Any, started_ms: Any) -> bool:
    """A registry entry counts while its pid may be the registering process: gone, zombie or a pid
    reused by a process that started after the session (a crash leaves the file behind) do not."""
    if type(pid) is not int or not 0 < pid < 2 ** 31:
        return False
    if procs.same_process_alive(pid, None) is False:
        return False
    started = procs.start_epoch(pid)
    return not (started is not None and isinstance(started_ms, (int, float)) and not isinstance(started_ms, bool)
                and started > started_ms / 1000 + 60)


def codex_widens(owner: Dict[str, Any], send: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Codex only: would a sent turn fall outside what the thread's owner accepts? An ordinary task's
    resumes check every turn of its thread (``permission_violation``), so a wider turn sent into it
    would block them for good."""
    allowed = {"read-only"} if requested_sandbox("codex", owner) in (None, "read-only") \
        else {"read-only", CODEX_WORKSPACE_WRITE}
    if full_access_enabled("codex", owner):
        allowed.add(CODEX_FULL_ACCESS)
    owner_network = network_enabled("codex", owner) or full_access_enabled("codex", owner)
    sandbox = requested_sandbox("codex", send)
    network = network_enabled("codex", send) or full_access_enabled("codex", send)
    if sandbox in allowed and (owner_network or not network):
        return None
    return {"owner_sandbox": requested_sandbox("codex", owner), "owner_network_access": owner_network,
            "sandbox": sandbox, "network_access": network}


def session_holder(engine: str, env: Dict[str, str], session_id: str) -> Optional[Dict[str, Any]]:
    """A live process outside this relay that owns the session, from the CLI's own records.

    Claude Code (observed 2.1.312) registers every running session, TUI and `-p` alike, in
    ``<config>/sessions/<pid>.json`` and takes no lock for ``--resume``: an open session neither sees
    a turn appended by another process nor keeps it, because its next turn forks the transcript.
    Codex (observed 0.162.1) holds ``thread-writer-locks/<id>.lock`` while a turn writes the thread
    and refuses a second writer. A lock that cannot be checked counts as held."""
    if engine == "claude":
        for path in sorted(glob.glob(str(claude_config_dir(env) / "sessions" / "*.json"))):
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("sessionId") != session_id:
                continue
            if _claude_registration_live(data.get("pid"), data.get("startedAt")):
                return {"holder": "claude_process", "pid": data["pid"], "record": path,
                        **{k: data[k] for k in ("kind", "entrypoint", "status") if isinstance(data.get(k), str)}}
        return None
    if engine == "codex":
        lock = codex_home(env) / "thread-writer-locks" / f"{session_id}.lock"
        try:
            fd = os.open(lock, os.O_RDONLY | os.O_NONBLOCK)  # never waits, not even on a non-file
        except FileNotFoundError:
            return None
        except OSError:
            return {"holder": "codex_thread_writer_lock", "record": str(lock), "lock": "unverifiable"}
        try:
            # Non-blocking shared probe, released at once: it conflicts only with a writer's exclusive
            # lock, works on a read-only descriptor, and never waits for or keeps another lock.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        except BlockingIOError:
            return {"holder": "codex_thread_writer_lock", "record": str(lock), "lock": "held"}
        except OSError:
            return {"holder": "codex_thread_writer_lock", "record": str(lock), "lock": "unverifiable"}
        finally:
            os.close(fd)
    return None


def session_preflight(engine: str, env: Dict[str, str], session_id: str, *, fixture_state_dir: str,
                      check_holder: bool = True) -> Optional[Dict[str, Any]]:
    """Before a turn is delivered to an existing session: the session exists on this machine, no live
    process outside the relay owns it, and a Codex thread was created by the built-in openai provider
    (its rollout records that once, so a foreign thread is checked before any turn is sent)."""
    if session_record(engine, env, session_id, fixture_state_dir=fixture_state_dir) is None:
        where = {"claude": str(claude_config_dir(env) / "projects" / "*" / f"{session_id}.jsonl"),
                 "codex": str(codex_home(env) / "sessions" / "*" / "*" / "*" / f"rollout-*{session_id}.jsonl"),
                 "fixture": str(Path(fixture_state_dir) / f"{session_id}.json")}[engine]
        return {"reason": "session_not_found", "session_id": session_id, "searched": where}
    holder = session_holder(engine, env, session_id) if check_holder else None
    if holder is not None:
        return {"reason": "session_busy", "session_id": session_id, **holder}
    provider = provider_violation(engine, env, session_id)
    if provider is not None:
        return {**provider, "session_id": session_id}
    return None


def session_problem_message(problem: Dict[str, Any]) -> str:
    sid, reason = problem.get("session_id"), problem.get("reason")
    if reason == "session_not_found":
        return f"session {sid} does not exist on this machine (searched {problem.get('searched')})"
    if reason == "session_busy" and problem.get("holder") == "claude_process":
        return (f"session {sid} is open in a live Claude Code process (pid {problem.get('pid')}); a turn appended "
                "now would not reach it and its next turn would fork the transcript; close that session first")
    if reason == "session_busy" and problem.get("holder") == "codex_thread_writer_lock":
        return (f"Codex thread {sid} has an active writer ({problem.get('lock')} thread-writer lock); "
                "wait until that session is idle")
    if reason == "session_busy":
        return (f"session {sid} is in use by relay task(s) {', '.join(problem.get('holder_task_ids', []))}; wait "
                "for them to end, or queue this message behind the one holding it with --after <task id> "
                "--after-require any_terminal")
    if reason == "session_permissions_wider":
        return (f"Codex thread {sid} belongs to relay task {problem.get('owner_task_id')} (sandbox="
                f"{problem.get('owner_sandbox')}, network_access={str(problem.get('owner_network_access')).lower()}); "
                f"a turn with sandbox={problem.get('sandbox')}, network_access="
                f"{str(problem.get('network_access')).lower()} would make that task's later resumes fail their "
                "permission check. Send with the same or narrower permissions, or submit a new task")
    return (f"Codex thread {sid} is not shown to have been created by the built-in openai provider "
            f"({reason}); refusing to continue it")


def codex_permission_checkpoint(env: Dict[str, str], session_id: str) -> Dict[str, Any]:
    """Capture the existing thread boundary before a resumed CLI can append its turn."""
    path = codex_rollout(env, session_id)
    if path is None:
        return {"missing": "rollout"}
    try:
        stat = path.stat()
        return {"path": str(path.resolve()), "dev": stat.st_dev, "ino": stat.st_ino, "offset": stat.st_size}
    except OSError as exc:
        return {"error": type(exc).__name__}


def provider_violation(engine: str, env: Dict[str, str], session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """After a run: Codex records the provider that served a thread in the thread's own rollout
    (``session_meta.model_provider``). Anything but the built-in ``openai`` provider, or a thread
    without that record, means the run is not shown to have been served by the subscription.
    The record is written when the thread is created, so a resumed turn relies on the preflight."""
    if engine != "codex" or not session_id:
        return None
    path = codex_rollout(env, session_id)
    if path is None:
        return {"reason": "provider_unverified", "missing": "rollout"}
    for ev in iter_json_lines(path):
        if ev.get("type") == "session_meta":
            payload = ev.get("payload")
            provider = payload.get("model_provider") if isinstance(payload, dict) else None
            if provider == "openai":
                return None
            return {"reason": "provider_not_subscription", "model_provider": provider}
    return {"reason": "provider_unverified", "missing": "session_meta"}


def requested_sandbox(engine: str, payload: Dict[str, Any]) -> Optional[str]:
    if engine != "codex":
        return None
    return (payload.get("options") or {}).get("sandbox") or "read-only"


def full_access_enabled(engine: str, payload: Dict[str, Any]) -> bool:
    """Both task options are required; a truthy string or integer is never an opt-in."""
    return (engine == "codex" and requested_sandbox(engine, payload) == CODEX_FULL_ACCESS
            and (payload.get("options") or {}).get("allow_full_access") is True)


def network_enabled(engine: str, payload: Dict[str, Any]) -> bool:
    """Network in the workspace-write sandbox needs both options, as full access does."""
    options = payload.get("options") or {}
    network = options.get("allow_network", options.get("network_access"))
    return (engine == "codex" and requested_sandbox(engine, payload) == CODEX_WORKSPACE_WRITE
            and network is True and options.get("network_access", network) is network)


def codex_network_override(engine: str, payload: Dict[str, Any]) -> Optional[str]:
    """The one network setting a workspace-write run gets from its task; nothing for other sandboxes."""
    if engine != "codex" or requested_sandbox(engine, payload) != CODEX_WORKSPACE_WRITE:
        return None
    return f"{CODEX_NETWORK_SETTING}={'true' if network_enabled(engine, payload) else 'false'}"


def completion_marker_required(payload: Dict[str, Any]) -> bool:
    return (payload.get("options") or {}).get("require_completion_marker") is True


def completion_marker(result: Any) -> Optional[str]:
    """The CLI's own completion report: ``complete`` or ``incomplete`` if the last non-empty line of
    its final message is exactly that marker line (surrounding whitespace aside), else None."""
    lines = [line.strip() for line in result.splitlines() if line.strip()] if isinstance(result, str) else []
    for marker in COMPLETION_MARKERS:
        if lines and lines[-1] == COMPLETION_LINE_PREFIX + marker:
            return marker
    return None


def requested_tools(engine: str, payload: Dict[str, Any]) -> List[str]:
    """Expand a validated task's named tools, directory grants and command prefixes into rules."""
    if engine != "claude":
        return []
    options = payload.get("options") or {}
    tools = options.get("allowed_tools", [])
    if not isinstance(tools, list):
        return []
    rules = {tool for tool in tools if isinstance(tool, str) and tool in CLAUDE_ALLOWABLE_TOOLS}
    for directory in options.get("allowed_write_dirs", []):
        rules.update((f"Edit({directory})", f"Write({directory})"))
    for command in options.get("allowed_bash_commands", []):
        rules.add(f"Bash({command}:*)")
    return sorted(rules)


def _outside(arg: str, cwd: str) -> bool:
    """Whether a path argument resolves (symlinks and .. included) outside the task directory."""
    root = os.path.realpath(cwd)
    path = os.path.realpath(arg if os.path.isabs(arg) else os.path.join(root, arg))
    return path != root and not path.startswith(root.rstrip(os.sep) + os.sep)


def bash_rule_warnings(engine: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Submit diagnostics only; never alter a request or its permission rules."""
    warnings: List[Dict[str, Any]] = []
    if engine != "claude":
        return warnings
    for command in (payload.get("options") or {}).get("allowed_bash_commands", []):
        name, *args = command.split()
        if name in CLAUDE_BASH_FLAG_RULE_COMMANDS and any(word.startswith("-") for word in args):
            warnings.append({"code": "bash_rule_not_honoured", "command": command,
                             "hint": f"Claude Code (observed 2.1.312) denies {name} with flags even when a matching "
                                     f"prefix rule is listed, also under Bash({name}:*), Bash({name} *) or an exact "
                                     "rule; only an unrestricted Bash rule, which cli-relay does not offer, allows "
                                     f"it. Use {name} <src> <dst> without flags (drop -n only when overwriting the "
                                     "destination is acceptable), or check existence with test -e first: run "
                                     f"`test -e <dst> || {name} <src> <dst>` and allow both the `test -e <dst>` and "
                                     f"`{name} <src> <dst>` prefixes."})
        cwd = payload.get("cwd")
        if not cwd or name not in CLAUDE_BASH_PATH_CHECKED_COMMANDS:
            continue
        outside = sorted({arg for arg in args if not arg.startswith("-") and _outside(arg, cwd)})
        if outside:
            checked = ", ".join(sorted(CLAUDE_BASH_PATH_CHECKED_COMMANDS))
            warnings.append({"code": "bash_rule_path_outside_cwd", "command": command, "paths": outside,
                             "hint": f"Claude Code (observed 2.1.312) denies {checked} when a path argument is "
                                     "outside the task directory, even when the prefix rule matches; use paths "
                                     "inside --cwd, or submit with a --cwd that contains every path the command "
                                     "needs."})
    return warnings


def permission_summary(details: Dict[str, Any]) -> str:
    """Exact task opt-ins, shared by submit receipts and notification messages."""
    opened = []
    if details.get("sandbox") not in (None, "read-only"):
        opened.append("sandbox=" + details["sandbox"])
    if details.get("full_access") is True:
        opened.append("full_access=true")
    if details.get("network_access") is True:
        opened.append("network_access=true")
    if details.get("allowed_tools"):
        opened.append("allowed_tools=" + ",".join(details["allowed_tools"]))
    return "; ".join(opened) or "default minimal"


def permission_details(engine: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Requested permissions for receipts and events, not proof that an attempt ran."""
    if engine == "codex":
        full = full_access_enabled(engine, payload)  # no sandbox at all, so the network too
        return {"sandbox": requested_sandbox(engine, payload), "full_access": full,
                "network_access": full or network_enabled(engine, payload)}
    if engine == "claude":
        return {"allowed_tools": requested_tools(engine, payload)}
    return {}


def request_details(engine: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """What a task asked for, for receipts, status and events: its permissions and whether success
    needs the completion marker."""
    details = permission_details(engine, payload)
    options = payload.get("options") or {}
    settings_warnings = policy.warnings(engine, payload)
    return {**details, "permissions": permission_summary(details),
            **policy.settings(engine, payload),
            **({"settings_warnings": settings_warnings} if settings_warnings else {}),
            "completion_marker_required": completion_marker_required(payload),
            **billing_policy(engine, options),
            **({"unset_env": options["unset_env"]} if "unset_env" in options else {}),
            **({"target_session_id": payload["target_session_id"]} if "target_session_id" in payload else {})}


def permission_violation(engine: str, env: Dict[str, str], session_id: Optional[str],
                         sandbox: Optional[str], *, permission_checkpoint: Optional[Dict[str, Any]] = None,
                         resume_session_id: Optional[str] = None,
                         allow_full_access: bool = False, allow_network: bool = False,
                         own_turns_only: bool = False) -> Optional[Dict[str, Any]]:
    """After a run: Codex records each turn's effective sandbox and any active named permission
    profile in the thread's rollout (``turn_context``). Every turn must have run in the requested
    sandbox or a narrower one, without network access unless the task opted in, and with no named
    profile; a thread without that record is not shown to have kept the requested permissions.
    ``own_turns_only`` (a message sent to an existing thread) checks only the turns after the
    attempt's checkpoint: earlier turns were not run by this task and are not its to certify."""
    if engine != "codex":
        return None
    if sandbox == CODEX_FULL_ACCESS and allow_full_access is not True:
        return {"reason": "full_access_not_opted_in", "requested": sandbox}
    if resume_session_id and session_id != resume_session_id:
        return {"reason": "permissions_unverified", "missing": "resumed_session"}
    if not session_id:
        return None
    path = codex_rollout(env, session_id)
    if path is None:
        return {"reason": "permissions_unverified", "missing": "rollout"}
    if resume_session_id:
        checkpoint = permission_checkpoint
        if (not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("path"), str)
                or any(type(checkpoint.get(k)) is not int or checkpoint[k] < 0 for k in ("dev", "ino", "offset"))):
            return {"reason": "permissions_unverified", "missing": "permission_checkpoint"}
        try:
            stat = path.stat()
            unchanged = (str(path.resolve()) == checkpoint["path"] and stat.st_dev == checkpoint["dev"]
                         and stat.st_ino == checkpoint["ino"] and stat.st_size >= checkpoint["offset"])
        except OSError:
            unchanged = False
        if not unchanged:
            return {"reason": "permissions_unverified", "missing": "unchanged_rollout"}
        # An old safe turn cannot certify a resumed attempt whose own context is absent.
        if not any(ev.get("type") == "turn_context" for ev in iter_json_lines(path, offset=checkpoint["offset"])):
            return {"reason": "permissions_unverified", "missing": "current_turn_context"}
    turns = []
    for ev in iter_json_lines(path, offset=permission_checkpoint["offset"] if resume_session_id and own_turns_only
                              else 0):
        if ev.get("type") == "turn_context":
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            policy = payload.get("sandbox_policy")
            kind = policy.get("type") if isinstance(policy, dict) else None
            # Codex 0.163.0 records workspace-write network access as a boolean and none for read-only
            # or full access. A missing key or false is "off"; no other value is taken for it.
            network = policy.get("network_access", False) if isinstance(policy, dict) else False
            turns.append((kind if isinstance(kind, str) else None, payload.get("active_permission_profile"), network))
    if not turns:
        return {"reason": "permissions_unverified", "missing": "turn_context"}
    allowed = {"read-only"} if sandbox in (None, "read-only") else {"read-only", "workspace-write"}
    if sandbox == CODEX_FULL_ACCESS and allow_full_access is True:
        allowed.add(CODEX_FULL_ACCESS)
    wider = sorted({str(kind) for kind, _, _ in turns if kind not in allowed})
    if wider:
        return {"reason": "sandbox_not_as_requested", "requested": sandbox or "read-only", "observed": wider}
    network_requested = ((sandbox == CODEX_WORKSPACE_WRITE and allow_network is True)
                         or (sandbox == CODEX_FULL_ACCESS and allow_full_access is True))
    with_network = sorted({str(kind) for kind, _, network in turns if network is not False})
    if with_network and not network_requested:
        return {"reason": "network_not_as_requested", "requested": sandbox or "read-only", "network_access": False,
                "observed": with_network}
    if any(profile not in (None, "") for _, profile, _ in turns):
        return {"reason": "named_permission_profile_active", "requested": sandbox or "read-only"}
    return None


def assert_safe(engine: str, argv: Iterable[str], *, options: Optional[Dict[str, Any]] = None,
                cwd: Optional[str] = None) -> None:
    args = list(argv)
    bad = sorted(set(args) & _FORBIDDEN[engine])
    if engine in ("codex", "claude"):
        bad = sorted({arg for arg in args if arg.partition("=")[0] in _FORBIDDEN[engine]})
    if engine == "claude" and any("bypasspermissions" in arg.lower() for arg in args):
        bad.append("bypassPermissions")
    if bad:
        raise RuntimeError(f"refusing to run {engine} with forbidden flags {bad}")
    sandboxes = set(CODEX_SANDBOXES)
    if full_access_enabled(engine, {"options": options}):
        sandboxes.add(CODEX_FULL_ACCESS)
    network = codex_network_override(engine, {"options": options})
    if engine == "claude":
        if options and any(key in options for key in ("allowed_tools", "allowed_write_dirs", "allowed_bash_commands")):
            try:
                validated = validate_options(engine, options, cwd=cwd)
            except UsageError as exc:
                raise RuntimeError("refusing unvalidated claude allowed tools") from exc
            options = validated
        modes = [args[i + 1] if i + 1 < len(args) else None for i, arg in enumerate(args)
                 if arg == "--permission-mode"]
        if modes != [CLAUDE_PERMISSION_MODE] or any(arg.startswith("--permission-mode=") for arg in args):
            raise RuntimeError("refusing to run claude without the explicit dontAsk permission mode")
    tools = ",".join(requested_tools(engine, {"options": options}))
    for i, arg in enumerate(args):
        sandbox = override = None
        if arg in ("--sandbox", "-s") and (i + 1 < len(args) or engine == "codex"):
            sandbox = args[i + 1] if i + 1 < len(args) else ""
        elif engine == "codex" and arg.startswith("--sandbox="):
            sandbox = arg.partition("=")[2]
        elif engine == "codex" and arg.startswith("-s") and not arg.startswith("--"):
            sandbox = arg[2:]
        if sandbox is not None and sandbox not in sandboxes:
            raise RuntimeError("refusing an unapproved codex sandbox; danger-full-access requires "
                               "sandbox=danger-full-access and allow_full_access=true on the task")
        if engine == "codex":
            if arg in ("-c", "--config"):
                override = args[i + 1] if i + 1 < len(args) else ""
            elif arg.startswith("--config="):
                override = arg.partition("=")[2]
            elif arg.startswith("-c") and not arg.startswith("--"):
                override = arg[2:]
        if override is not None:
            allowed = {f'sandbox_mode="{s}"' for s in sandboxes}
            allowed.update(f'model_reasoning_effort="{e}"' for e in EFFORT_LEVELS)
            if network:  # only the value built from the task's validated options, never another one
                allowed.add(network)
            if override.replace(" ", "") not in allowed:
                raise RuntimeError("refusing a codex config override other than an approved sandbox, a validated "
                                   "effort or the task's own workspace network setting")
        if engine == "claude" and arg.startswith("--settings="):
            raise RuntimeError("refusing unvalidated claude settings")
        if engine == "claude" and arg == "--settings":
            try:
                setting = json.loads(args[i + 1])
            except (IndexError, ValueError):
                setting = None
            if not any(setting == {"env": {CLAUDE_EFFORT_ENV: e}} for e in EFFORT_LEVELS):
                raise RuntimeError("refusing claude settings other than a task-local effort override")
        if engine == "claude" and arg == "--effort":
            if i + 1 == len(args) or args[i + 1] not in EFFORT_LEVELS:
                raise RuntimeError("refusing an unvalidated claude effort")
        if engine == "claude" and arg.partition("=")[0] in CLAUDE_ALLOWED_TOOLS_FLAGS:
            # Only the one value built from the task's validated list. The flag is variadic, so a
            # further value right after it would be one more allow rule.
            if (not tools or arg not in CLAUDE_ALLOWED_TOOLS_FLAGS or args[i + 1:i + 2] != [tools]
                    or any(not a.startswith("--") for a in args[i + 2:i + 3])):
                raise RuntimeError("refusing claude allowed tools other than the task's validated allowed_tools")


def build(engine: str, *, payload: Dict[str, Any], attempt: Dict[str, Any], run_dir: Path,
          fixture_state_dir: str, env: Dict[str, str]) -> Spec:
    cwd = payload["cwd"]
    opts = validate_options(engine, payload.get("options", {}), cwd=cwd)
    resume = attempt.get("resume_session_id")
    cenv = child_env(env, unset_env=task_unset_env(engine, opts))
    last_message = None
    evidence: Dict[str, Any] = {}
    if engine == "claude":
        binary = resolve_bin(engine, env)
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        argv += ["--resume", resume] if resume else ["--session-id", attempt["native_session_id"]]
        argv += ["--permission-mode", CLAUDE_PERMISSION_MODE, "--permission-prompts", "none"]
        tools = requested_tools(engine, {"options": opts})
        if tools:
            # Allow rules for this session only: deny and ask rules of every settings layer, managed
            # included, still win; the mode, the settings files and their sources stay as they are.
            argv += ["--allowedTools", ",".join(tools)]
        if opts.get("model"):
            argv += ["--model", opts["model"]]
        if opts.get("effort"):
            # Settings env can override the shell and --effort. This generated one-key CLI
            # settings layer wins over user/project values without changing auth or any rules.
            argv += ["--effort", opts["effort"], "--settings",
                     json.dumps({"env": {CLAUDE_EFFORT_ENV: opts["effort"]}}, separators=(",", ":"))]
    elif engine == "codex":
        binary = resolve_bin(engine, env)
        last_message = str(run_dir / "last_message.txt")
        # Always explicit, so neither the first run nor a resume takes the sandbox (or the network
        # access of workspace-write) from whatever the configuration says at that moment.
        sandbox = opts.get("sandbox") or "read-only"
        network = codex_network_override(engine, {"options": opts})
        network_args = ["-c", network] if network else []
        if resume:
            # `codex exec resume` has no --sandbox of its own: pin it both as the exec-level option
            # and as a config override for the resumed thread.
            argv = [binary, "exec", "--sandbox", sandbox, "resume", "-c", f'sandbox_mode="{sandbox}"',
                    *network_args, "--json", "-o", last_message]
        else:
            argv = [binary, "exec", "--json", "-C", cwd, "-o", last_message, "--sandbox", sandbox, *network_args]
        skip_git_repo_check = opts.get("skip_git_repo_check")
        if skip_git_repo_check is True:
            decision = "explicit_skip"
        elif skip_git_repo_check is False or git_state.is_work_tree(cwd):
            decision = "enforced"
        else:
            decision = "auto_skipped"
        evidence["codex_git_repo_check"] = decision
        if decision != "enforced":
            argv.append("--skip-git-repo-check")
        if opts.get("model"):
            argv += ["-m", opts["model"]]
        if opts.get("effort"):
            argv += ["-c", f'model_reasoning_effort="{opts["effort"]}"']
        argv += [resume, "-"] if resume else ["-"]
    else:
        argv = [sys.executable, "-m", "cli_relay.fixture", "--state-dir", fixture_state_dir]
        argv += ["--resume", resume] if resume else ["--session-id", attempt["native_session_id"]]
        argv += ["--duration", str(opts.get("duration_s", 0)), "--exit-code", str(opts.get("exit_code", 0))]
        if "result" in opts:
            argv += ["--result", opts["result"]]
        if opts.get("side_effect_file"):
            argv += ["--side-effect-file", opts["side_effect_file"]]
        if opts.get("no_result"):
            argv.append("--no-result")
        if opts.get("api_key_source"):
            argv += ["--api-key-source", opts["api_key_source"]]
        if opts.get("permission_mode"):
            argv += ["--permission-mode-reported", opts["permission_mode"]]
        if opts.get("descendant_s"):
            argv += ["--descendant", str(opts["descendant_s"])]
            if opts.get("descendant_pid_file"):
                argv += ["--descendant-pid-file", opts["descendant_pid_file"]]
        pkg_parent = str(Path(__file__).resolve().parent.parent)
        cenv["PYTHONPATH"] = pkg_parent + (os.pathsep + cenv["PYTHONPATH"] if cenv.get("PYTHONPATH") else "")
        cenv["CLI_RELAY_ATTEMPT_ID"] = attempt["attempt_id"]
    if not argv[0]:
        raise RuntimeError(f"{engine} CLI not found")
    assert_safe(engine, argv, options=opts, cwd=cwd)
    return Spec(argv=argv, env=cenv, cwd=cwd, last_message_path=last_message, evidence=evidence)


# ---------------------------------------------------------------------------- output parsing
def codex_git_repo_check_warning(stderr_path: Path) -> Optional[Dict[str, str]]:
    """Export only a fixed hint, never any of the CLI's stderr."""
    try:
        with open(stderr_path, "rb") as f:
            if not any(b"Not inside a trusted directory" in line for line in f):
                return None
    except OSError:
        return None
    return {"code": "codex_trusted_directory",
            "hint": "Codex rejected the task directory. Submit a new task with --option "
                    "skip_git_repo_check=true to pass --skip-git-repo-check."}


def codex_session_busy_warning(stderr_path: Path) -> Optional[Dict[str, str]]:
    """Codex refused a resume because another process writes the thread. Only a fixed hint is exported."""
    try:
        with open(stderr_path, "rb") as f:
            if not any(CODEX_ACTIVE_WRITER in line for line in f):
                return None
    except OSError:
        return None
    return {"code": "session_busy",
            "hint": "Codex refused to resume the thread because another process is writing to it (thread-writer "
                    "lock). Wait until that session is idle, then resume this task or submit a new key."}


def iter_json_lines(path: Path, *, offset: int = 0) -> Iterable[Dict[str, Any]]:
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        f.seek(offset)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


class StreamWatcher:
    """Incrementally parses newline-delimited JSON appended to a file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.partial = b""

    def poll(self) -> List[Dict[str, Any]]:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(1 << 20)
        except OSError:
            return []
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        out = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out


def session_id_from_event(engine: str, ev: Dict[str, Any]) -> Optional[str]:
    if engine == "codex":
        for key in ("thread_id", "session_id", "conversation_id"):
            if isinstance(ev.get(key), str) and ev.get("type") in ("thread.started", "session.created",
                                                                    "session_configured", None):
                return ev[key]
        msg = ev.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "session_configured" and isinstance(msg.get("session_id"), str):
            return msg["session_id"]
        return None
    if ev.get("type") == "system" and ev.get("subtype") == "init" and isinstance(ev.get("session_id"), str):
        return ev["session_id"]
    return None


def billing_violation(engine: str, ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Reject a Claude-format API-key/bypass init, and require the real Claude adapter's fixed
    permission mode. The fixture keeps its independent permission-denial scenarios."""
    if engine in ("claude", "fixture") and ev.get("type") == "system" and ev.get("subtype") == "init":
        source = ev.get("apiKeySource")
        if source not in (None, "none"):
            return {"reason": "api_key_source_detected", "apiKeySource": source}
        if ev.get("permissionMode") == "bypassPermissions":
            return {"reason": "bypass_permissions_mode", "permissionMode": ev["permissionMode"]}
        if engine == "claude" and ev.get("permissionMode") != CLAUDE_PERMISSION_MODE:
            return {"reason": "permission_mode_not_as_requested", "requested": CLAUDE_PERMISSION_MODE,
                    "observed": ev.get("permissionMode")}
    return None


def parse_output(engine: str, stdout_path: Path, last_message_path: Optional[Path] = None, *,
                 allowed_bash_commands: Iterable[str] = ()) -> Dict[str, Any]:
    """Summarise the CLI's own output. ``native`` is None when no final result was emitted."""
    init: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    completed = failed = False
    error: Optional[str] = None
    last_message: Optional[str] = None
    for ev in iter_json_lines(stdout_path):
        sid = session_id_from_event(engine, ev)
        if sid and not session_id:
            session_id = sid
        if engine == "codex":
            etype = ev.get("type")
            if etype == "turn.completed":
                completed = True
            elif etype == "turn.failed":
                failed = True
                err = ev.get("error")
                error = err.get("message") if isinstance(err, dict) else str(err)
            elif etype == "error":
                error = str(ev.get("message"))
            item = ev.get("item")
            if etype == "item.completed" and isinstance(item, dict) and item.get("type") in ("agent_message",
                                                                                              "assistant_message"):
                if isinstance(item.get("text"), str):
                    last_message = item["text"]
        else:
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                init = {k: ev.get(k) for k in ("session_id", "apiKeySource", "permissionMode", "model")}
            elif ev.get("type") == "result":
                result = ev
    native: Optional[Dict[str, Any]] = None
    denied_allowed_commands = set()
    if engine == "codex":
        if last_message_path is not None and last_message is None:
            try:
                text = Path(last_message_path).read_text(encoding="utf-8")
                last_message = text if text.strip() else None
            except OSError:
                pass
        if completed or failed:
            native = {"ok": completed and not failed, "status": "failed" if failed else "completed",
                      "thread_id": session_id, "result": last_message, "error": error}
    elif result is not None:
        denials = result.get("permission_denials") or []
        if not isinstance(denials, list):
            denials = []
        names = []
        for d in denials:
            if isinstance(d, dict):
                names.append(str(d.get("tool_name") or d.get("tool") or "unknown"))
                tool_input = d.get("tool_input")
                command = tool_input.get("command") if isinstance(tool_input, dict) else None
                if engine == "claude" and d.get("tool_name") == "Bash" and isinstance(command, str):
                    command = command.strip()
                    for entry in allowed_bash_commands:
                        if command == entry or command.startswith(entry + " "):
                            denied_allowed_commands.add(entry[:200])
        native = {
            "ok": bool(result.get("subtype") == "success" and not result.get("is_error")),
            "subtype": result.get("subtype"),
            "is_error": result.get("is_error"),
            "result": result.get("result") if isinstance(result.get("result"), str) else None,
            "session_id": result.get("session_id"),
            "num_turns": result.get("num_turns"),
            "permission_denials": names,
        }
    diagnostics = {"warnings": [{"code": "allowed_rule_denied", "commands": sorted(denied_allowed_commands)}]} \
        if denied_allowed_commands else {}
    return {"native": native, "session_id": session_id, "init": init, **diagnostics}


def classify(native: Optional[Dict[str, Any]], *, exit_code: Optional[int], exit_signal: Optional[int],
             exit_known: bool, cancel_requested: bool) -> str:
    """CLI exit status and the CLI's own result are recorded separately; this only names the outcome.
    It never claims business success. A native success with a known zero exit can recover from
    denied tools; those denials are warnings. Otherwise a structured denial is ``needs_input``,
    even if the CLI reports an error (only Claude reports structured denials)."""
    if cancel_requested and (exit_signal is not None or (exit_known and exit_code != 0)
                             or native is None or not native.get("ok")):
        return "cancelled"
    if native is not None and native.get("ok") and exit_known and exit_code == 0 and exit_signal is None:
        return "succeeded"
    if native is not None and native.get("permission_denials"):
        return "needs_input"
    if native is None or not native.get("ok"):
        return "failed"
    if exit_known and exit_code != 0:
        return "failed"
    return "succeeded"


def decide(engine: str, parsed: Dict[str, Any], *, env: Dict[str, str], session_id: Optional[str],
           exit_code: Optional[int], exit_signal: Optional[int], exit_known: bool, cancel_requested: bool,
           guard: Optional[Dict[str, Any]] = None, sandbox: Optional[str] = None,
           permission_checkpoint: Optional[Dict[str, Any]] = None,
           resume_session_id: Optional[str] = None,
           allow_full_access: bool = False, allow_network: bool = False,
           require_completion_marker: bool = False, own_turns_only: bool = False) -> Tuple[str, Dict[str, Any]]:
    """Name an attempt's outcome from what the run left behind; returns (outcome, evidence).

    The keeper and reconciliation both call this, so the outcome does not depend on which of
    them closes the attempt. A run shown not to be served by the subscription, or not shown to
    have kept the requested sandbox, is ``blocked`` whatever else happened: ``guard`` is what
    the keeper saw while the run was live (also persisted in exit.json); the init event of the
    saved output and the Codex thread record are re-read here, so the fact survives a keeper
    that died before committing.

    With ``require_completion_marker`` a run that would be ``succeeded`` is ``needs_input``
    unless the CLI's own final message ends with the complete marker line: the incomplete
    line, any other last line and no final message all mean the CLI did not report the task
    done. The marker never changes any other outcome."""
    if guard is None and engine in ("claude", "fixture") and parsed.get("init"):
        guard = billing_violation(engine, {"type": "system", "subtype": "init", **parsed["init"]})
    if guard is not None:
        return "blocked", {"billing_guard": guard}
    if engine == "claude" and not parsed.get("init"):
        return "blocked", {"permission_check": {"reason": "permissions_unverified", "missing": "init"}}
    provider = provider_violation(engine, env, session_id)
    if provider is not None:
        return "blocked", {"provider_check": provider}
    permissions = permission_violation(engine, env, session_id, sandbox,
                                       permission_checkpoint=permission_checkpoint, resume_session_id=resume_session_id,
                                       allow_full_access=allow_full_access, allow_network=allow_network,
                                       own_turns_only=own_turns_only)
    if permissions is not None:
        return "blocked", {"permission_check": permissions}
    native = parsed.get("native")
    outcome = classify(native, exit_code=exit_code, exit_signal=exit_signal, exit_known=exit_known,
                       cancel_requested=cancel_requested)
    if require_completion_marker is not True:
        return outcome, {}
    check: Dict[str, Any] = {"marker": completion_marker((native or {}).get("result"))}
    if outcome == "succeeded" and check["marker"] != "complete":
        return "needs_input", {"completion_check": {**check, "downgraded_from": "succeeded"}}
    return outcome, {"completion_check": check}


def native_trace_exists(engine: str, session_id: Optional[str]) -> Optional[bool]:
    """Best-effort evidence that the CLI created its own session record (never used to decide a re-run)."""
    if not session_id or engine == "fixture":
        return None
    env = dict(os.environ)
    if engine == "claude":
        pattern = str(claude_config_dir(env) / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    else:
        pattern = str(codex_home(env) / "sessions" / "**" / f"*{glob.escape(session_id)}*.jsonl")
    try:
        return bool(glob.glob(pattern, recursive=True))
    except OSError:
        return None

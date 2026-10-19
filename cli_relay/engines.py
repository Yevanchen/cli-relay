"""Engine adapters for the official CLIs (and a deterministic fixture).

Each adapter: validates options, builds argv (never via a shell), checks the auth path
before spawning, and parses the CLI's own machine-readable output. No adapter adds a
permission-relaxing flag; ``assert_safe`` re-checks every argv before it is used.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import UsageError

ENGINES = ("claude", "codex", "fixture")

# Variables that route a CLI to API-key or third-party billing. Presence blocks the run;
# only the variable *names* are ever reported.
BILLING_ENV: Dict[str, tuple] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BEDROCK_BASE_URL",
               "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "AZURE_OPENAI_API_KEY"),
    "fixture": (),
}

# Markers of a calling Claude Code session; they must not leak into a child CLI.
SCRUB_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_PROJECT_DIR", "CLAUDE_PID")

_FORBIDDEN = {
    "claude": {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--bare", "--continue",
               "-c", "--permission-mode", "--fork-session"},
    "codex": {"--dangerously-bypass-approvals-and-sandbox", "--approve-for-me", "--last", "--full-auto",
              "--dangerously-bypass-hook-trust", "--yolo"},
    "fixture": set(),
}

_OPTION_TYPES: Dict[str, Dict[str, tuple]] = {
    "claude": {"model": (str,)},
    "codex": {"model": (str,), "sandbox": (str,), "skip_git_repo_check": (bool,)},
    "fixture": {"duration_s": (int, float), "exit_code": (int,), "result": (str,), "side_effect_file": (str,),
                "no_result": (bool,), "api_key_source": (str,), "permission_mode": (str,),
                "descendant_s": (int, float), "descendant_pid_file": (str,)},
}

CODEX_SANDBOXES = ("read-only", "workspace-write")

# Configuration layers outside the user's own files that the CLIs also read.
CODEX_SYSTEM_CONFIG = (Path("/etc/codex/config.toml"),)
CODEX_MANAGED_CONFIG = (Path("/etc/codex/managed_config.toml"),)
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


def validate_engine(engine: str) -> str:
    if engine not in ENGINES:
        raise UsageError(f"unknown engine {engine!r}", allowed=list(ENGINES))
    return engine


def validate_options(engine: str, options: Dict[str, Any]) -> Dict[str, Any]:
    spec = _OPTION_TYPES[engine]
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
    if engine == "codex" and "sandbox" in clean and clean["sandbox"] not in CODEX_SANDBOXES:
        raise UsageError("codex sandbox must be read-only or workspace-write (danger-full-access is never used)",
                         allowed=list(CODEX_SANDBOXES))
    for key in ("side_effect_file", "descendant_pid_file"):
        if engine == "fixture" and key in clean and not os.path.isabs(clean[key]):
            raise UsageError(f"{key} must be an absolute path")
    return clean


def resolve_bin(engine: str, env: Dict[str, str]) -> Optional[str]:
    override = env.get({"claude": "CLI_RELAY_CLAUDE_BIN", "codex": "CLI_RELAY_CODEX_BIN"}[engine])
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which(engine, path=env.get("PATH"))


def child_env(env: Dict[str, str]) -> Dict[str, str]:
    return {k: v for k, v in env.items() if k not in SCRUB_ENV and not k.startswith("CLI_RELAY_TEST_")}


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


def _dirs_up(cwd: Optional[str]) -> List[Path]:
    """The task directory and all of its parents, outermost first."""
    if not cwd:
        return []
    start = Path(os.path.abspath(cwd))
    return [*reversed(start.parents), start]


def _existing_once(layers: Iterable[Tuple[str, Path]]) -> List[Tuple[str, Path]]:
    seen, out = set(), []
    for layer, path in layers:
        real = os.path.realpath(path)
        if real not in seen and os.path.isfile(path):
            seen.add(real)
            out.append((layer, path))
    return out


def _merge(base: Dict[str, Any], top: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def codex_config_problems(env: Dict[str, str], cwd: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Merge the Codex configuration layers (system, user, every ``.codex/config.toml`` from the
    filesystem root down to the task directory, managed) and name every setting that could serve the
    run from anything but the built-in OpenAI provider with the ChatGPT sign-in. Project layers are
    read whether or not Codex trusts the project, and an unreadable layer is a problem: when the
    configuration cannot be confirmed, the run is blocked. Only key names are reported."""
    layers = [("system", p) for p in CODEX_SYSTEM_CONFIG] + [("user", codex_home(env) / "config.toml")]
    layers += [("project", d / ".codex" / "config.toml") for d in _dirs_up(cwd)]
    layers += [("managed", p) for p in CODEX_MANAGED_CONFIG]
    merged: Dict[str, Any] = {}
    problems: List[str] = []
    read: List[str] = []
    for layer, path in _existing_once(layers):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            problems.append(f"{layer}:unreadable")
            continue
        read.append(layer)
        merged = _merge(merged, data)
    tables: List[Tuple[str, Dict[str, Any]]] = [("", merged)]
    if "profile" in merged:
        profile = (merged.get("profiles") or {}).get(merged["profile"])
        if isinstance(profile, dict):
            tables.append(("profiles.<active>.", profile))
        else:
            problems.append("profile")
    provider = "openai"
    for prefix, table in tables:
        provider = table.get("model_provider", provider)
        problems += [prefix + key for key in sorted(table) if key.endswith("base_url")]
        for key in ("forced_login_method", "preferred_auth_method"):
            if table.get(key) not in (None, "chatgpt"):
                problems.append(prefix + key)
    if provider != "openai":
        problems.append("model_provider")
    if "openai" in (merged.get("model_providers") or {}):
        problems.append("model_providers.openai")
    return problems, read


def claude_settings_problems(env: Dict[str, str], cwd: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """Name every Claude settings layer (user, ``.claude/settings[.local].json`` of the task directory
    and its parents, managed) that sets a credential helper, a non-claude.ai login or a billing
    variable in its ``env``. Only key and variable names are reported."""
    layers = [("user", claude_config_dir(env) / "settings.json")]
    for d in _dirs_up(cwd):
        layers += [("project", d / ".claude" / "settings.json"), ("project", d / ".claude" / "settings.local.json")]
    layers += [("managed", p) for p in CLAUDE_MANAGED_SETTINGS]
    problems: List[str] = []
    read: List[str] = []
    for layer, path in _existing_once(layers):
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
        settings_env = data.get("env") if isinstance(data.get("env"), dict) else {}
        problems += [f"{layer}:env.{name}" for name in BILLING_ENV["claude"] if settings_env.get(name)]
        permissions = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        if permissions.get("defaultMode") == "bypassPermissions":  # nothing would ever be denied
            problems.append(f"{layer}:permissions.defaultMode")
    return problems, read


def preflight(engine: str, env: Dict[str, str], cwd: Optional[str] = None) -> Preflight:
    """Everything that decides which account serves the run is checked before the CLI starts:
    billing variables, the CLI's configuration layers that apply to ``cwd``, and the login."""
    present = [name for name in BILLING_ENV[engine] if env.get(name)]
    if present:
        return Preflight(False, "billing_env_present", {"names": present})
    if engine == "fixture":
        return Preflight(True)
    check = codex_config_problems if engine == "codex" else claude_settings_problems
    problems, layers = check(env, cwd)
    if problems:
        return Preflight(False, "config_not_subscription", {"settings": problems})
    binary = resolve_bin(engine, env)
    if not binary:
        return Preflight(False, "cli_not_found", {"engine": engine})
    argv = [binary, "auth", "status"] if engine == "claude" else [binary, "login", "status"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=child_env(env),
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return Preflight(False, "auth_unverified", {"error": type(exc).__name__})
    if engine == "claude":
        info = _first_json_object(out.stdout) or {}
        auth = {k: info.get(k) for k in ("loggedIn", "authMethod", "apiProvider", "subscriptionType")}
        if (info.get("loggedIn") is True and info.get("authMethod") == "claude.ai"
                and info.get("apiProvider") in (None, "firstParty")):
            return Preflight(True, None, {"auth": auth, "config_layers": layers})
        return Preflight(False, "auth_not_subscription", {"auth": auth, "returncode": out.returncode})
    text = (out.stdout + "\n" + out.stderr).lower()
    if "api key" in text:
        return Preflight(False, "auth_not_subscription", {"auth": "api_key"})
    if out.returncode == 0 and "chatgpt" in text:
        return Preflight(True, None, {"auth": "chatgpt", "config_layers": layers})
    return Preflight(False, "auth_unverified", {"auth": "unknown", "returncode": out.returncode})


def codex_rollout(env: Dict[str, str], thread_id: str) -> Optional[Path]:
    matches = glob.glob(str(codex_home(env) / "sessions" / "*" / "*" / "*" / f"rollout-*{glob.escape(thread_id)}.jsonl"))
    return Path(sorted(matches)[0]) if matches else None


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
            provider = (ev.get("payload") or {}).get("model_provider")
            if provider == "openai":
                return None
            return {"reason": "provider_not_subscription", "model_provider": provider}
    return {"reason": "provider_unverified", "missing": "session_meta"}


def assert_safe(engine: str, argv: Iterable[str]) -> None:
    args = list(argv)
    bad = sorted(set(args) & _FORBIDDEN[engine])
    if bad:
        raise RuntimeError(f"refusing to run {engine} with forbidden flags {bad}")
    for i, arg in enumerate(args):
        if arg in ("--sandbox", "-s") and i + 1 < len(args) and args[i + 1] not in CODEX_SANDBOXES:
            raise RuntimeError("refusing to run codex with a sandbox other than read-only/workspace-write")
        if engine == "codex" and arg in ("-c", "--config") and i + 1 < len(args):
            override = args[i + 1].replace(" ", "")
            allowed = {f'sandbox_mode="{s}"' for s in CODEX_SANDBOXES}
            if override not in allowed:
                raise RuntimeError("refusing a codex config override other than a read-only/workspace-write sandbox")


def build(engine: str, *, payload: Dict[str, Any], attempt: Dict[str, Any], run_dir: Path,
          fixture_state_dir: str, env: Dict[str, str]) -> Spec:
    opts = payload.get("options", {})
    cwd = payload["cwd"]
    resume = attempt.get("resume_session_id")
    cenv = child_env(env)
    last_message = None
    if engine == "claude":
        binary = resolve_bin(engine, env)
        argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        argv += ["--resume", resume] if resume else ["--session-id", attempt["native_session_id"]]
        argv += ["--permission-prompts", "none"]
        if opts.get("model"):
            argv += ["--model", opts["model"]]
    elif engine == "codex":
        binary = resolve_bin(engine, env)
        last_message = str(run_dir / "last_message.txt")
        # Always explicit, so neither the first run nor a resume takes the sandbox from whatever
        # the configuration says at that moment.
        sandbox = opts.get("sandbox") or "read-only"
        if resume:
            # `codex exec resume` has no --sandbox of its own: pin it both as the exec-level option
            # and as a config override for the resumed thread.
            argv = [binary, "exec", "--sandbox", sandbox, "resume", "-c", f'sandbox_mode="{sandbox}"',
                    "--json", "-o", last_message]
        else:
            argv = [binary, "exec", "--json", "-C", cwd, "-o", last_message, "--sandbox", sandbox]
        if opts.get("skip_git_repo_check"):
            argv.append("--skip-git-repo-check")
        if opts.get("model"):
            argv += ["-m", opts["model"]]
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
    assert_safe(engine, argv)
    return Spec(argv=argv, env=cenv, cwd=cwd, last_message_path=last_message)


# ---------------------------------------------------------------------------- output parsing
def iter_json_lines(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
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
    """A Claude-format init event that reports an API-key source means the run is not on the
    subscription; one that reports bypassPermissions means nothing would be denied."""
    if engine in ("claude", "fixture") and ev.get("type") == "system" and ev.get("subtype") == "init":
        source = ev.get("apiKeySource")
        if source not in (None, "none"):
            return {"reason": "api_key_source_detected", "apiKeySource": source}
        if ev.get("permissionMode") == "bypassPermissions":
            return {"reason": "bypass_permissions_mode", "permissionMode": ev["permissionMode"]}
    return None


def parse_output(engine: str, stdout_path: Path, last_message_path: Optional[Path] = None) -> Dict[str, Any]:
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
        names = []
        for d in denials:
            if isinstance(d, dict):
                names.append(str(d.get("tool_name") or d.get("tool") or "unknown"))
        native = {
            "ok": bool(result.get("subtype") == "success" and not result.get("is_error")),
            "subtype": result.get("subtype"),
            "is_error": result.get("is_error"),
            "result": result.get("result") if isinstance(result.get("result"), str) else None,
            "session_id": result.get("session_id"),
            "num_turns": result.get("num_turns"),
            "permission_denials": names,
        }
    return {"native": native, "session_id": session_id, "init": init}


def classify(native: Optional[Dict[str, Any]], *, exit_code: Optional[int], exit_signal: Optional[int],
             exit_known: bool, cancel_requested: bool) -> str:
    """CLI exit status and the CLI's own result are recorded separately; this only names the outcome.
    It never claims business success. A structured permission denial means the run stopped at a
    permission nobody could grant, so it is ``needs_input`` even if the CLI also reports an error
    (only Claude reports denials; Codex has no structured denial in its JSON output)."""
    if cancel_requested and (exit_signal is not None or (exit_known and exit_code != 0)
                             or native is None or not native.get("ok")):
        return "cancelled"
    if native is not None and native.get("permission_denials"):
        return "needs_input"
    if native is None or not native.get("ok"):
        return "failed"
    if exit_known and exit_code != 0:
        return "failed"
    return "succeeded"


def decide(engine: str, parsed: Dict[str, Any], *, env: Dict[str, str], session_id: Optional[str],
           exit_code: Optional[int], exit_signal: Optional[int], exit_known: bool, cancel_requested: bool,
           guard: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """Name an attempt's outcome from what the run left behind; returns (outcome, evidence).

    The keeper and reconciliation both call this, so the outcome does not depend on which of
    them closes the attempt. A run shown not to be served by the subscription is ``blocked``
    whatever else happened: ``guard`` is what the keeper saw while the run was live (also
    persisted in exit.json); the init event of the saved output and the Codex thread record
    are re-read here, so the fact survives a keeper that died before committing."""
    if guard is None and engine in ("claude", "fixture") and parsed.get("init"):
        guard = billing_violation(engine, {"type": "system", "subtype": "init", **parsed["init"]})
    if guard is not None:
        return "blocked", {"billing_guard": guard}
    provider = provider_violation(engine, env, session_id)
    if provider is not None:
        return "blocked", {"provider_check": provider}
    return classify(parsed.get("native"), exit_code=exit_code, exit_signal=exit_signal, exit_known=exit_known,
                    cancel_requested=cancel_requested), {}


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

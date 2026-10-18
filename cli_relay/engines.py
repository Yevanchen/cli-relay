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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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
                "no_result": (bool,), "api_key_source": (str,)},
}

CODEX_SANDBOXES = ("read-only", "workspace-write")


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
    if engine == "fixture" and "side_effect_file" in clean and not os.path.isabs(clean["side_effect_file"]):
        raise UsageError("side_effect_file must be an absolute path")
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


def preflight(engine: str, env: Dict[str, str]) -> Preflight:
    present = [name for name in BILLING_ENV[engine] if env.get(name)]
    if present:
        return Preflight(False, "billing_env_present", {"names": present})
    if engine == "fixture":
        return Preflight(True)
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
            return Preflight(True, None, {"auth": auth})
        return Preflight(False, "auth_not_subscription", {"auth": auth, "returncode": out.returncode})
    text = (out.stdout + "\n" + out.stderr).lower()
    if "api key" in text:
        return Preflight(False, "auth_not_subscription", {"auth": "api_key"})
    if out.returncode == 0 and "chatgpt" in text:
        return Preflight(True, None, {"auth": "chatgpt"})
    return Preflight(False, "auth_unverified", {"auth": "unknown", "returncode": out.returncode})


def assert_safe(engine: str, argv: Iterable[str]) -> None:
    args = list(argv)
    bad = sorted(set(args) & _FORBIDDEN[engine])
    if bad:
        raise RuntimeError(f"refusing to run {engine} with forbidden flags {bad}")
    for i, arg in enumerate(args):
        if arg in ("--sandbox", "-s") and i + 1 < len(args) and args[i + 1] not in CODEX_SANDBOXES:
            raise RuntimeError("refusing to run codex with a sandbox other than read-only/workspace-write")


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
        if resume:
            argv = [binary, "exec", "resume", "--json", "-o", last_message]
        else:
            argv = [binary, "exec", "--json", "-C", cwd, "-o", last_message]
            if opts.get("sandbox"):
                argv += ["--sandbox", opts["sandbox"]]
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
    """A Claude-format init event that reports an API-key source means the run is not on the subscription."""
    if engine in ("claude", "fixture") and ev.get("type") == "system" and ev.get("subtype") == "init":
        source = ev.get("apiKeySource")
        if source not in (None, "none"):
            return {"reason": "api_key_source_detected", "apiKeySource": source}
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
    It never claims business success."""
    if cancel_requested and (exit_signal is not None or (exit_known and exit_code != 0)
                             or native is None or not native.get("ok")):
        return "cancelled"
    if native is None or not native.get("ok"):
        return "failed"
    if exit_known and exit_code != 0:
        return "failed"
    if native.get("permission_denials"):
        return "needs_input"
    return "succeeded"


def native_trace_exists(engine: str, session_id: Optional[str]) -> Optional[bool]:
    """Best-effort evidence that the CLI created its own session record (never used to decide a re-run)."""
    if not session_id or engine == "fixture":
        return None
    home = Path.home()
    if engine == "claude":
        pattern = str(home / ".claude" / "projects" / "*" / f"{glob.escape(session_id)}.jsonl")
    else:
        pattern = str(home / ".codex" / "sessions" / "**" / f"*{glob.escape(session_id)}*.jsonl")
    try:
        return bool(glob.glob(pattern, recursive=True))
    except OSError:
        return None

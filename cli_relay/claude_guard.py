"""A deliberately narrow, fail-closed ceiling for Claude's native subagents.

The hook program and policy live in argv, not in a task-writable script/settings file.
This is a tool policy, not an OS sandbox. Only read-only ``ls`` Bash grants are supported
in this first guarded implementation: an interpreter could otherwise change the CLI's
configuration behind its tool permission checker.
"""
from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unicodedata
from typing import Any

from .errors import UsageError

VERIFIED_VERSION = "2.1.301"
WORKER = "cli-relay-worker"
TOOLS = ("Read", "Glob", "Grep", "Bash", "Write", "Edit", "NotebookEdit")
OPTIONAL_TOOLS = ("TodoWrite", "WebFetch", "WebSearch")
CONTROL_TOOLS = ("Task", "Agent")


def enabled(options: dict) -> bool:
    return bool(set(options.get("allowed_tools", [])) & set(CONTROL_TOOLS))


def validate(options: dict) -> None:
    if any(command != "ls" for command in options.get("allowed_bash_commands", [])):
        raise UsageError("Task/Agent currently support only --allow-bash ls (or no Bash grant); "
                         "other commands need an outer sandbox; see docs/claude-subagents.md")


def protected_scope_overlap(first: Path, second: Path) -> bool:
    """Conservatively protect case/normalization aliases, including on macOS volumes.

    Only deny decisions use this comparison. File grants keep their strict realpath checks;
    folding a grant match would incorrectly authorize distinct paths on Linux.
    """
    a, b = (unicodedata.normalize("NFC", str(path)).casefold().rstrip("/") for path in (first, second))
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def check_version(binary: str, env: dict) -> None:
    try:
        result = subprocess.run([binary, "--version"], env=env, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("cannot verify the Claude subagent guard version") from exc
    if result.returncode or result.stdout.strip() != VERIFIED_VERSION + " (Claude Code)":
        raise RuntimeError("Task/Agent require the live-verified Claude Code " + VERIFIED_VERSION +
                           "; rerun native acceptance before changing this pin")


def config_problems(data: dict) -> list[str]:
    """Loaded executable customizations could disable or bypass the policy hook."""
    problems = [key for key in ("hooks", "disableAllHooks", "agent", "enabledPlugins", "mods",
                                "extraKnownMarketplaces") if data.get(key)]
    if data.get("allowManagedHooksOnly"):
        problems.append("allowManagedHooksOnly")
    # A settings env can re-enable safe/simple mode or teams after we clean the shell env.
    for name in data.get("env", {}):
        if name.startswith(("CLAUDE_", "CLAUDECODE", "BASH_FUNC_", "LD_", "DYLD_")) or name in (
                "BASH_ENV", "ENV", "ZDOTDIR", "PATH", "HOME", "SHELL", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS"):
            if name != "CLAUDE_CODE_EFFORT_LEVEL":
                problems.append("env." + name)
    return problems


def clean_env(env: dict) -> dict:
    keep = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL"}
    result = {key: value for key, value in env.items()
            if not (key.startswith(("CLAUDE_", "CLAUDECODE", "BASH_FUNC_")) and key not in keep)
            and key not in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS")}
    # A task-writable directory on PATH must not turn `ls` into an arbitrary program.
    result["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    result["SHELL"] = "/bin/bash"
    return result


def evaluate(event: dict, policy: dict) -> str | None:
    """Return a denial reason. This function is also the standalone inline hook program."""
    from pathlib import Path
    import shlex
    import stat

    if event.get("hook_event_name") != "PreToolUse":
        return "unexpected hook event"
    if event.get("permission_mode") != "dontAsk":
        return "permission mode must remain dontAsk"
    if Path(event.get("cwd", "")).resolve() != Path(policy["cwd"]):
        return "working directory changed"
    tool = event.get("tool_name")
    args = event.get("tool_input")
    if not isinstance(args, dict):
        return "invalid tool input"
    if tool in ("Task", "Agent"):
        if args.get("subagent_type") != "cli-relay-worker":
            return "only the relay-generated cli-relay-worker may run"
        # No resume, fork, worktree, team, model, tools or mode override on delegation.
        if set(args) - {"description", "prompt", "subagent_type", "run_in_background"}:
            return "subagent overrides and resume are disabled"
        if event.get("agent_id"):
            return "nested delegation is disabled"
        if args.get("run_in_background"):
            return "background delegation is disabled; use concurrent foreground workers"
        return None
    if tool == "Bash":
        command = args.get("command", "")
        # One simple ls command: no shell program, expansions, substitutions or redirection.
        if (not isinstance(command, str) or not command or any(c in command for c in
                "\n\r\x00;&|<>$`\\(){}") or args.get("dangerouslyDisableSandbox")):
            return "shell syntax or sandbox override is outside the Bash ceiling"
        try:
            words = shlex.split(command)
        except ValueError:
            return "invalid Bash command"
        if not words or words[0] != "ls" or "ls" not in policy["bash"]:
            return "command is outside the Bash ceiling"
        return None
    if tool in ("Write", "Edit", "NotebookEdit"):
        raw = args.get("notebook_path" if tool == "NotebookEdit" else "file_path")
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            return "invalid write path"
        path = Path(raw)
        if not path.is_absolute():
            path = Path(policy["cwd"]) / path
        # Keep CLI/tool configuration and git control files protected, including under a grant.
        protected = (".claude", ".claude.json", ".git", ".mcp.json", ".bashrc", ".bash_profile", ".bash_login", ".profile",
                     ".zshenv", ".zshrc", ".zprofile", ".zlogin", ".zlogout")
        if any(part.casefold() in protected for part in path.parts):
            return "configuration writes are disabled"
        resolved = path.resolve()
        if any(part.casefold() in protected for part in resolved.parts):
            return "configuration writes are disabled"
        if not any(resolved != Path(root) and resolved.is_relative_to(Path(root)) for root in policy["write"]):
            return "write is outside the directory ceiling"
        # realpath alone cannot spot a pre-existing hard link to a protected outside file.
        if resolved.exists():
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return "write target must be a regular file with one link"
        return None
    if tool not in policy["tools"]:
        return "tool is outside the task ceiling"
    return None


def snapshot(options: dict, cwd: str) -> dict:
    validate(options)
    root = Path(cwd).resolve()
    return {"cwd": str(root), "bash": options.get("allowed_bash_commands", []),
            "write": [str((root / path.removesuffix("/**")).resolve())
                      for path in options.get("allowed_write_dirs", [])],
            "tools": ["Read", "Glob", "Grep", *[t for t in OPTIONAL_TOOLS if t in options.get("allowed_tools", [])]]}


def settings(options: dict, cwd: str) -> dict:
    encoded = base64.b64encode(json.dumps(snapshot(options, cwd), sort_keys=True).encode()).decode()
    program = ("import json,sys,base64\n" + inspect.getsource(evaluate) +
               "\ntry:\n reason=evaluate(json.load(sys.stdin),json.loads(base64.b64decode(sys.argv[1])))\n"
               "except BaseException:\n reason='invalid guard input or policy'\n"
               "if reason:\n print('cli-relay ceiling: '+reason,file=sys.stderr)\n sys.exit(2)\n")
    result: dict[str, Any] = {
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{
            "type": "command", "command": shlex.join([sys.executable, "-I", "-c", program, encoded]),
            "timeout": 10, "onFailure": "block",
        }]}]},
        "permissions": {"disableBypassPermissionsMode": "disable"},
    }
    if options.get("effort"):
        result["env"] = {"CLAUDE_CODE_EFFORT_LEVEL": options["effort"]}
    return result


def agents(options: dict) -> dict:
    tools = [*TOOLS, *[t for t in OPTIONAL_TOOLS if t in options.get("allowed_tools", [])]]
    return {WORKER: {"description": "Relay worker for bounded subtasks; multiple workers may run concurrently.",
                     "prompt": "Perform the assigned subtask using the available tools. Report permission denials.",
                     "tools": tools, "permissionMode": "dontAsk", "maxTurns": 12}}


def flags(options: dict, cwd: str) -> list[str]:
    tools = [*TOOLS, *CONTROL_TOOLS, *[t for t in OPTIONAL_TOOLS if t in options.get("allowed_tools", [])]]
    return ["--settings", json.dumps(settings(options, cwd), separators=(",", ":")),
            "--agents", json.dumps(agents(options), separators=(",", ":")),
            "--tools", ",".join(tools), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-chrome"]


def assert_flags(argv: list[str], options: dict, cwd: str) -> None:
    expected = flags(options, cwd)
    pair_names = {"--settings", "--agents", "--tools", "--mcp-config"}
    pairs = {flag: expected[expected.index(flag) + 1] for flag in pair_names}
    singles = {"--strict-mcp-config", "--disable-slash-commands", "--no-chrome"}
    for flag, value in pairs.items():
        if argv.count(flag) != 1 or argv[argv.index(flag) + 1:argv.index(flag) + 2] != [value]:
            raise RuntimeError("refusing missing or changed subagent ceiling flag " + flag)
        if argv[argv.index(flag) + 2:argv.index(flag) + 3] and not argv[argv.index(flag) + 2].startswith("--"):
            raise RuntimeError("refusing extra values after subagent ceiling flag " + flag)
    if any(argv.count(flag) != 1 for flag in singles):
        raise RuntimeError("refusing missing subagent isolation flags")
    protected = set(pairs) | singles
    if any(arg.partition("=")[0] in protected and arg not in protected for arg in argv):
        raise RuntimeError("refusing alternate spellings of subagent ceiling flags")
    if any(arg.partition("=")[0] in {"--agent", "--plugin-dir", "--plugin-url", "--add-dir",
                                     "--safe-mode", "--restricted", "--permission-prompt-tool"} for arg in argv):
        raise RuntimeError("refusing a subagent customization override")

# Claude subagent permission ceiling

Verified with native Claude Code **2.1.301** on macOS, 2026-11-12. This draft enables
the exact `Task` and `Agent` option names after live boundary tests passed. The supported
scope is deliberately limited: **concurrent foreground `cli-relay-worker` agents, file
writes inside the task's directory grants, and either no Bash grant or `--allow-bash ls`.**
Other Bash grants are rejected when combined with Task/Agent. Ordinary tasks without these
opt-ins retain their existing Bash prefix behavior and now explicitly disable native
Task/Agent with `--disallowedTools Task,Agent`.

## Why the parent permission mode is insufficient

The [official subagent permission documentation](https://code.claude.com/docs/en/sub-agents#permission-modes)
allows a custom definition to choose `acceptEdits` under a `dontAsk` parent. The documented
bypass exception does not cover that mode. In our exploratory live test, an `acceptEdits`
child really did report that mode to the hook; the hook blocked its outside write.
The parent's init event alone therefore cannot establish a child permission ceiling.

## Enforcement

`cli_relay/claude_guard.py` supplies a single session-wide `PreToolUse` hook via inline
`--settings`. The executable program and immutable task policy are in argv: there is no
task-writable hook script or settings file to replace. The same evaluator runs for parent
and child calls. As documented for [session-wide subagent hooks](https://code.claude.com/docs/en/sub-agents#define-hooks-for-subagents),
tool hooks also run inside subagents. Each rejected action exits **2**. `onFailure: "block"`
covers interpreter failure and timeout; see the [official failure contract](https://code.claude.com/docs/en/hooks#block-the-action-when-a-hook-fails).
We also tested a nonexistent hook executable with a real allowed Write call: the file was
not created and Claude reported a permission denial.

The evaluator enforces these conditions independently of an inherited broad allow rule:

- Every call must report `dontAsk` and the original cwd. A different mode is denied.
- Write, Edit and NotebookEdit resolve the target at call time and require a file strictly
  beneath an immutable granted directory. Outside paths, symlink escapes, existing hard
  links and special files are denied. CLI/git configuration and shell startup files are
  protected even beneath a grant.
- Bash accepts one simple `ls` command only when the parent requested `ls`. Shell control
  syntax, substitution, redirection and sandbox overrides are denied. PATH is fixed to
  system directories and startup environment overrides are removed, so a task cannot
  replace `ls` with a program it writes into its output directory.
- Agent/Task can invoke only the relay's CLI-generated `cli-relay-worker`. Its definition
  fixes `permissionMode: dontAsk` and an explicit tool list with no delegation tools.
  Arbitrary built-in, user, project and plugin agent types are denied **before dispatch**.
  Custom definitions may be discovered, but they cannot be invoked. A project definition
  with the same worker name did not replace the CLI definition in live acceptance.
- Delegation cannot pass mode, model, tool, isolation or other override fields. Background
  workers, nested agents, individual child resumes, forks, worktrees and agent teams are
  denied. Two foreground calls can still execute concurrently.
- Parent and worker tool lists exclude mode-changing tools, skills and alternative command
  tools. External MCP configuration, slash commands and Chrome integration are disabled for
  guarded attempts. Unknown tools are denied by the hook.

Preflight and build reject executable customizations that could bypass/disable the hook:
existing hooks, enabled plugins/mods, a selected custom session agent, hook-disabling
settings, and relevant settings environment overrides. They also reject write grants
overlapping the Claude config directory or the Python guard runtime. This does **not**
silently drop the user's permission policy: settings sources remain loaded and deny/ask
rules remain in force. [Permission rule precedence](https://code.claude.com/docs/en/permissions#precedence-among-rules-hooks-and-mods)
still applies; hook success does not approve a call or bypass a deny/ask rule.

The CLI version must match **2.1.301 exactly**, both in preflight and before build. Another
version blocks before model execution until native acceptance is repeated and the pin is
reviewed. Argv validation rejects missing, changed, duplicated or alternate-form guard
flags. Effort overrides share the one generated settings object. First run, exact parent
resume and fresh rerun rebuild the ceiling from the stored task options; child resumes are
not supported. The attempt records the requested policy under `claude_subagent_guard`.

## Usage

```sh
cli-relay submit --key parallel-report --engine claude --cwd ~/code/app \
  --task-class highest_quality --resource-class standard \
  --option 'model="claude-opus-5-5"' --option 'effort="max"' \
  --option 'allowed_tools=["Task","Agent","TodoWrite"]' \
  --allow-bash ls --allow-write-dir ./out \
  --prompt 'Use two cli-relay-worker foreground agents concurrently, with run_in_background false. Have each list the directory and write its report into out.'
```

WebFetch/WebSearch remain separate exact-name opt-ins. A task that needs arbitrary Bash
programs and subagents together requires a separately designed outer sandbox/broker. Do
not broaden this guard's command parser into a general shell permission checker.
Each worker has a 12-turn limit. If it returns partial work, delegate a new bounded subtask;
individual child resumes remain disabled.

## Live acceptance

All probes used `claude -p` and the existing **claude.ai Max subscription**, never
`ANTHROPIC_API_KEY`. Final acceptance used `engines.preflight` and `engines.build`, not a
handwritten approximation of the launcher. Credentials were temporarily copied from the
existing macOS Keychain login into a private isolated config directory, reported as
`authMethod: claude.ai`, and deleted afterwards. No global Claude configuration was edited.
All file targets were disposable `/private/tmp` directories.

The commands, prompts, raw stream output, stderr and filesystem checks are retained in the
job's `out/acceptance/`. A compact reviewable transcript and raw-log hashes are committed as
[acceptance evidence](acceptance/claude-subagents-20261112.json).

| Probe | Observed result |
| --- | --- |
| Both workers call allowed `ls` | Success |
| Each tries `touch outside-bash-*` | Hook denial; files absent |
| Each writes `outside-write-*.txt` | Hook denial; files absent |
| Each writes its own `out/worker-*.txt` | Files contain `A` and `B`; worker lifetimes overlap |
| Project agents declare acceptEdits, bypassPermissions or auto | All three dispatches denied; outside files absent |
| Conflicting project definition named cli-relay-worker | Relay CLI definition used; forbidden collision file absent |
| Parent writes `out/parent-ok.txt` | Success |
| Hook executable is missing | Allowed Write blocked; file absent |

An early exploratory background run timed out with one unfinished worker. It is retained
as an unsuccessful probe, not counted as a parallel pass. The implemented contract uses
foreground workers; final acceptance completed both and checked native tool events and
actual files, rather than relying on the model's report.

## Limits and future work

This is a native tool policy for a pinned CLI on a trusted host, not an OS sandbox against
another local process changing files concurrently, administrator policy changes, or a
modified Claude binary. Reads follow the parent's existing read policy. The ceiling can
be stricter than the parent grant: configuration writes and non-ls Bash grants cannot be
used for guarded tasks. Existing enabled plugins/hooks must be removed from that task's
configuration before it can start; the relay does not edit global configuration for you.

Before expanding to arbitrary commands, other CLI versions, background/team/fork/nested
agents or individual child resumes, add a supported enforcement mechanism and repeat live
acceptance for that scope. Automated regressions run the real inline hook without a model
and cover validation, argv tampering, environment/config conflicts, links, mode changes,
first launch, parent resume and fresh rerun. They do not substitute for native acceptance.

# Claude subagent permission investigation

Checked on 2026-11-12 for the request to accept
`allowed_tools=["Task","Agent","WebFetch","WebSearch","TodoWrite"]`.

## Decision

Enable only the exact `TodoWrite` name alongside the existing web tools. Keep `Task` and
`Agent` opt-ins rejected, with an explanation in the usage error and `doctor` output.
This is the requested safety fallback, not completed support for parallel subagents.

The adapter's Bash prefix and Edit/Write directory rules are unchanged. Passing their
existing values to the parent CLI does **not** establish that every child keeps `dontAsk`.
The relay currently cannot enforce that condition, so it must not claim it.

## Evidence

- [Subagent permission modes](https://code.claude.com/docs/en/sub-agents#permission-modes):
  a definition without `permissionMode` inherits the parent mode. With a `dontAsk` parent,
  a definition can select another mode, including `acceptEdits` or available `auto`.
  The documented exception for `bypassPermissions` alone does not close these paths.
- [Permission mode behavior](https://code.claude.com/docs/en/permission-modes#available-modes):
  `acceptEdits` approves file changes and common filesystem commands. This can permit a write
  elsewhere inside cwd even when the parent opted into only `./out/**`. Auto mode makes
  approval decisions rather than limiting execution to the parent's preapproved prefixes.
- [CLI flags](https://code.claude.com/docs/en/cli-reference#cli-flags): `--allowedTools`
  controls approvals, whereas `--tools` controls tool availability. Print mode with
  `--permission-prompts none` denies unanswered prompts; it does not undo actions another
  permission mode already approves.
- [Tools reference](https://code.claude.com/docs/en/tools-reference#agent-tool-behavior):
  Agent itself needs no approval in Manual mode; its child calls undergo permission checks.
  Rejecting a name in the relay's option does not remove that tool from Claude Code.
- [SubagentStart hooks](https://code.claude.com/docs/en/hooks#subagentstart) cannot block a
  launch. Injecting a reminder there is not an enforcement mechanism.

For example, a user/project agent with `tools: Edit, Write, Bash` and
`permissionMode: acceptEdits` is sufficient to invalidate the inference that every child
shares the parent's approval mode. These are documented behaviors, not a claim that an
unauthorized write was reproduced on this machine.

Local read-only checks: `claude --version` reported **2.1.301**; `claude --help` advertised
`-p`, `--allowedTools`, `--agents`, `--permission-mode`, `--permission-prompts` and `--settings`.
Help describes the flags but does not promise immutable child permission inheritance.
The linked documentation can evolve independently of that installed version.

## What the relay currently checks

`cli_relay/engines.py` validates exact names, derives scoped rules in `requested_tools`, and
builds one Claude invocation for each attempt. It pins `dontAsk` and suppresses permission
prompts on first runs and resumes. `assert_safe` revalidates all permission lists before
accepting argv, so an invalid stored name cannot be silently filtered out and accepted.
The runtime permission check verifies the parent's init event; it does not observe or lock
every child mode. Same-session execution and inherited settings alone are insufficient.

Existing machine allow rules can add permissions independently of task opt-ins. An allowed
interpreter can write beyond Edit/Write directories; these rules are not an OS sandbox.
This change adds no new claim that all native subagent execution is blocked or isolated.

## Gate for a later implementation

1. Enforce an immutable child permission ceiling using a supported Claude mechanism, or an
   outer sandbox/broker that enforces the same command and write-directory grants. Version-gate
   that contract; fail closed when it cannot be verified.
2. Cover built-in, user/project/plugin agents, definition overrides, foreground/background
   execution, forks, nested delegation and resumed children. Account for already-running
   children and definition changes after preflight. Agent teams require separate coverage.
3. Keep inherited deny/ask and managed policies effective. Do not solve this with bypass mode,
   broad Bash/Edit/Write allow rules, dropped settings layers or prompt instructions.
4. Run isolated native acceptance cases: an allowed command and in-directory write succeed;
   an unapproved command and a write outside the grant fail under `dontAsk`, `acceptEdits`,
   `auto` and bypass overrides. Test exact resume and fresh rerun, and inspect child evidence.
   Only then add exact `Task` / `Agent` opt-ins and enable parallel work.

The regression suite verifies the relay's validation, argv, persistence and unchanged scoped
rules using fake CLIs and temporary ledgers. It does not simulate Claude's permission evaluator
or prove native subagent inheritance. No live model calls, install or deployment are required
for this safety-fallback PR.

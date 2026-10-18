"""Start a keeper fully detached from the caller (double fork + setsid).

The keeper must survive the caller's SSH/terminal disconnect and must never become a
zombie of a long-lived caller, so it is re-parented to init immediately.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import RelayError


class SpawnFailed(RelayError):
    code = "keeper_spawn_failed"
    exit_code = 1


def spawn_keeper(home: Path, task_id: str, attempt_id: str) -> None:
    home = Path(home)
    log_dir = home / "runs" / task_id
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = log_dir / f"keeper-{attempt_id}.log"
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["CLI_RELAY_KEEPER_DETACHED"] = "1"
    argv = [sys.executable, "-m", "cli_relay", "--home", str(home), "_keeper", task_id, attempt_id]
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:  # intermediate child: new session, fork the keeper, exit at once
        try:
            os.setsid()
            if os.fork() == 0:
                devnull = os.open(os.devnull, os.O_RDONLY)
                logfd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.dup2(devnull, 0)
                os.dup2(logfd, 1)
                os.dup2(logfd, 2)
                os.closerange(3, 1024)
                os.chdir(str(home))
                os.execve(sys.executable, argv, env)
            os._exit(0)
        except BaseException:
            os._exit(127)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise SpawnFailed("could not start the keeper process", task_id=task_id, attempt_id=attempt_id)

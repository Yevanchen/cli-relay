"""Error types. Each carries a stable machine-readable code and a process exit code."""

from __future__ import annotations

from typing import Any, Dict


class RelayError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: Dict[str, Any] = detail

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": False, "error": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


class UsageError(RelayError):
    code = "usage"
    exit_code = 2


class Conflict(RelayError):
    code = "conflict"
    exit_code = 3


class NotFound(RelayError):
    code = "not_found"
    exit_code = 4


class LedgerError(RelayError):
    code = "ledger_error"
    exit_code = 5


class LedgerNotInitialized(LedgerError):
    code = "ledger_not_initialized"


class LedgerMissing(LedgerError):
    code = "ledger_missing"


class LedgerCorrupt(LedgerError):
    code = "ledger_corrupt"


class LedgerWriteFailed(LedgerError):
    code = "ledger_write_failed"


class InsufficientDisk(LedgerError):
    code = "insufficient_disk"


class Refused(RelayError):
    """A precondition does not hold (e.g. resume while the old run may be alive)."""

    code = "refused"
    exit_code = 6

    def __init__(self, reason: str, message: str, **detail: Any) -> None:
        super().__init__(message, **detail)
        self.code = reason


class AckMismatch(Refused):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__("ack_mismatch", message, **detail)

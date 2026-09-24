"""Connection-scoped admission receipts, separate from RPC request lifetimes."""

from dataclasses import dataclass, field
import hashlib
import json
import threading

from reuleauxcoder.app.rpc.models import Submission
from reuleauxcoder.infrastructure.rpc.peer import RpcError


@dataclass
class Admission:
    fingerprint: str
    generation: int
    lock: threading.Lock = field(default_factory=threading.Lock)
    status: str | None = None
    receipt: Submission | None = None
    error: BaseException | None = None


class SubmissionAdmissions:
    def __init__(self, *, capacity: int = 4096):
        self._lock = threading.Lock()
        self._entries: dict[str, Admission] = {}
        self._capacity = capacity
        self._generation = -1

    def get(self, submission_id: str, value, *, generation: int) -> Admission:
        if not isinstance(submission_id, str) or not 1 <= len(submission_id) <= 128:
            raise RpcError(-32602, "Invalid submission ID")
        fingerprint = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
        with self._lock:
            if generation < self._generation:
                raise RpcError(-32002, "Session changed; submission was not accepted")
            self._generation = generation
            entry = self._entries.get(submission_id)
            if entry is not None:
                if entry.fingerprint != fingerprint or entry.generation != generation:
                    raise RpcError(-32602, "Submission ID reused for different input or session")
                return entry
            # A generation change invalidates retries before this lookup. Retain
            # every ID in the current generation; never evict one and resubmit it.
            self._entries = {
                key: item for key, item in self._entries.items()
                if item.generation == generation
            }
            if len(self._entries) >= self._capacity:
                raise RpcError(-32002, "Submission receipt limit reached; start a new session")
            entry = Admission(fingerprint, generation)
            self._entries[submission_id] = entry
            return entry

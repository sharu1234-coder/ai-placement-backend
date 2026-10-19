"""In-memory registry of live realtime voice sessions (VA-2).

Deliberately not a database table. These records are ephemeral transport state -- who is
connected, how much audio has arrived, which resume token is outstanding -- and persisting them
would create a retention surface for voice metadata that no feature needs and that the privacy
section of the specification argues against. No migration accompanies this phase for the same
reason.

**Known limitation:** process-local. A multi-worker deployment would need shared state for
resume to work across workers. There is no deployment yet (VA-0 established there is no
Dockerfile or deployment image at all), so this is recorded rather than solved -- solving it
now would mean choosing a backing store to support a topology nobody has chosen.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Final

#: How many simultaneous voice sessions one student may hold. More than a couple means a
#: client that is leaking connections, and each one pins a socket and a buffer.
DEFAULT_MAX_SESSIONS_PER_USER: Final[int] = 3

#: How long a resume token stays valid after the socket drops. Long enough to cover a tunnel
#: change or a lift; short enough that a stolen token is useless by the time it is found.
DEFAULT_RESUME_TTL_SECONDS: Final[float] = 300.0

#: How many unredeemed resume grants one student may hold. Live sessions are capped above, but
#: a grant deliberately *outlives* its session -- that is what makes resume possible -- so the
#: session cap does not bound this map. Without a cap of its own, an authenticated client that
#: connects and drops in a loop accumulates one grant per connection for the whole TTL window,
#: which is growth a client controls. Per account rather than global, so one noisy client
#: cannot evict anyone else's grant and turn their recoverable drop into a lost session.
DEFAULT_MAX_RESUME_GRANTS_PER_USER: Final[int] = 8


class TooManySessions(RuntimeError):
    """This user already holds the maximum number of concurrent sessions."""


@dataclass
class VoiceSessionRecord:
    """One live connection.

    Counters rather than content: byte totals and turn counts, never audio and never a
    transcript. There is nothing here that would be sensitive if it were logged.
    """

    session_id: str
    user_id: int
    sample_rate_hz: int
    channel_count: int
    opened_at: float
    last_seen_at: float
    audio_bytes: int = 0
    audio_frames: int = 0
    turns_committed: int = 0
    interrupts: int = 0
    cancels_requested: int = 0
    resumed_from: str | None = None
    closed: bool = False


@dataclass
class _ResumeGrant:
    token: str
    session_id: str
    user_id: int
    expires_at: float


@dataclass
class SessionRegistry:
    """Tracks live sessions and outstanding resume grants.

    Guarded by a lock because the test client drives the app from a portal thread while the
    event loop runs elsewhere, and a production server may serve several connections
    concurrently. The critical sections are all short.
    """

    max_sessions_per_user: int = DEFAULT_MAX_SESSIONS_PER_USER
    resume_ttl_seconds: float = DEFAULT_RESUME_TTL_SECONDS
    max_resume_grants_per_user: int = DEFAULT_MAX_RESUME_GRANTS_PER_USER
    clock: Callable[[], float] = time.monotonic

    _sessions: dict[str, VoiceSessionRecord] = field(default_factory=dict)
    _grants: dict[str, _ResumeGrant] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ lifecycle

    def open(self, *, user_id: int, sample_rate_hz: int, channel_count: int) -> VoiceSessionRecord:
        now = self.clock()
        with self._lock:
            live = [s for s in self._sessions.values() if s.user_id == user_id and not s.closed]
            if len(live) >= self.max_sessions_per_user:
                raise TooManySessions(
                    f"user {user_id} already holds {len(live)} voice sessions"
                )
            record = VoiceSessionRecord(
                session_id=uuid.uuid4().hex,
                user_id=user_id,
                sample_rate_hz=sample_rate_hz,
                channel_count=channel_count,
                opened_at=now,
                last_seen_at=now,
            )
            self._sessions[record.session_id] = record
            return record

    def close(self, session_id: str) -> None:
        """Mark a session closed and drop it.

        Its resume grant deliberately outlives it: the grant is the whole point of being able
        to come back after the socket died.
        """
        with self._lock:
            record = self._sessions.pop(session_id, None)
            if record is not None:
                record.closed = True

    def touch(self, session_id: str) -> None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is not None:
                record.last_seen_at = self.clock()

    def get(self, session_id: str) -> VoiceSessionRecord | None:
        with self._lock:
            return self._sessions.get(session_id)

    def active_for_user(self, user_id: int) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.user_id == user_id and not s.closed)

    @property
    def live_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    # ------------------------------------------------------------------ resume

    def issue_resume_token(self, record: VoiceSessionRecord) -> str:
        """Mint a single-use grant for this session."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_expired_locked()
            self._grants[token] = _ResumeGrant(
                token=token,
                session_id=record.session_id,
                user_id=record.user_id,
                expires_at=self.clock() + self.resume_ttl_seconds,
            )
            self._enforce_grant_quota_locked(record.user_id)
        return token

    def _enforce_grant_quota_locked(self, user_id: int) -> None:
        """Drop this user's oldest unredeemed grants beyond their quota.

        The one just minted is the newest, so it always survives: a client reconnecting in a
        loop loses its stalest recovery option, never the one it is about to use.
        """
        mine = [t for t, g in self._grants.items() if g.user_id == user_id]
        for stale in mine[: max(0, len(mine) - self.max_resume_grants_per_user)]:
            del self._grants[stale]

    def redeem_resume_token(self, token: str, *, user_id: int) -> _ResumeGrant | None:
        """Consume a grant, or return None.

        The owner check is the security boundary: a grant belongs to the user it was minted
        for, so a stolen or guessed token is useless to anyone else even while it is unexpired.
        Single use -- a redeemed grant is removed, so a replayed token buys nothing.
        """
        with self._lock:
            self._purge_expired_locked()
            grant = self._grants.get(token)
            if grant is None:
                return None
            if grant.user_id != user_id:
                # Deliberately indistinguishable from "no such token" to the caller: telling
                # the difference would confirm that someone else's token exists.
                return None
            del self._grants[token]
            return grant

    def _purge_expired_locked(self) -> None:
        now = self.clock()
        for token in [t for t, g in self._grants.items() if g.expires_at <= now]:
            del self._grants[token]

    @property
    def outstanding_grant_count(self) -> int:
        with self._lock:
            self._purge_expired_locked()
            return len(self._grants)

    def reset(self) -> None:
        """Drop all state. For tests, and for nothing else."""
        with self._lock:
            self._sessions.clear()
            self._grants.clear()


#: Process-wide registry. A module-level singleton rather than a FastAPI dependency default,
#: because ruff's B008 forbids a call in an argument default and because the registry's whole
#: purpose is to be shared across connections.
registry = SessionRegistry()

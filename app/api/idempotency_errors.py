"""One place to turn an idempotency outcome into an HTTP response.

Follows app/api/ai_errors.py: the routes that use the replay guard would otherwise each repeat
the same three `except` clauses, and three copies is how the status codes drift apart.

The status codes are chosen so a client can tell the three apart without reading message text:

409 Conflict -- the key already names a different request, or one too old to replay. The
    request as sent cannot be honoured, and retrying it unchanged will not help; the client
    needs a new key, which in practice means the student taking the action again.

409 with a distinct message -- the same operation is still running. Deliberately not 202: a
    202 would say "accepted, keep waiting" and the client has no handle to poll with. Phase 23's
    classifier already renders a 409 as "That changed while you were working. Please refresh and
    try again.", which is close enough to true here and, importantly, is not retryable -- so an
    automatic retry loop cannot turn one slow operation into a stampede.

400 Bad Request -- the key itself is malformed. That is a client bug, not a user error, and it
    is reported separately so it shows up as one in logs rather than hiding among conflicts.

Nothing here logs the key. It is not a secret, but it is per-operation client state with no
diagnostic value once the operation is identified, and the operation name is already logged.
"""

import logging

from fastapi import HTTPException, status

from app.services.idempotency import (
    IdempotencyConflictError,
    InvalidIdempotencyKeyError,
    OperationInProgressError,
)


def idempotency_http_error(
    exc: Exception, logger: logging.Logger, operation: str
) -> HTTPException:
    """The HTTPException for an idempotency failure, for the route to raise.

    Returns rather than raises, so the call site reads as `raise idempotency_http_error(...)`
    and the control flow stays visible in the route.
    """
    if isinstance(exc, InvalidIdempotencyKeyError):
        logger.info("rejected a malformed Idempotency-Key (operation=%s)", operation)
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, OperationInProgressError):
        logger.info("replay arrived while operation=%s was still running", operation)
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, IdempotencyConflictError):
        logger.info("idempotency conflict on operation=%s", operation)
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    raise exc

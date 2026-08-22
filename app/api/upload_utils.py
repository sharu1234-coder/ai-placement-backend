from fastapi import HTTPException, UploadFile, status

_CHUNK_SIZE = 1024 * 1024  # 1MB


async def read_upload_limited(file: UploadFile, max_bytes: int, too_large_detail: str) -> bytes:
    """Reads an UploadFile in chunks, aborting as soon as the cumulative size exceeds
    max_bytes. Unlike `await file.read()` followed by a size check, this never buffers
    more than ~max_bytes + one chunk in memory regardless of what the client sends --
    a plain post-hoc size check still lets an oversized body be fully buffered first."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=too_large_detail
            )
        chunks.append(chunk)
    return b"".join(chunks)

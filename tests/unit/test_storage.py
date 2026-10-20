from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.core.config import settings
from app.storage.local_provider import LocalStorageProvider
from app.storage.provider import StorageError
from app.storage.s3_provider import S3StorageProvider


def test_local_storage_save_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    storage = LocalStorageProvider()

    key = storage.save("resumes/test.pdf", b"hello world")

    assert key == "resumes/test.pdf"
    saved_path = tmp_path / "resumes" / "test.pdf"
    assert saved_path.read_bytes() == b"hello world"

    storage.delete(key)
    assert not saved_path.exists()


def test_local_storage_delete_missing_key_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    storage = LocalStorageProvider()

    storage.delete("resumes/does-not-exist.pdf")


def test_local_storage_load_returns_saved_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    storage = LocalStorageProvider()
    storage.save("resumes/test.pdf", b"hello world")

    assert storage.load("resumes/test.pdf") == b"hello world"


def test_local_storage_load_missing_key_raises_storage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    storage = LocalStorageProvider()

    with pytest.raises(StorageError):
        storage.load("resumes/does-not-exist.pdf")


def test_s3_storage_requires_bucket(monkeypatch):
    monkeypatch.setattr(settings, "storage_s3_bucket", None)

    with pytest.raises(StorageError, match="STORAGE_S3_BUCKET"):
        S3StorageProvider()


def _client_error(operation_name: str) -> ClientError:
    return ClientError({"Error": {"Code": "500", "Message": "boom"}}, operation_name)


@pytest.fixture()
def s3_provider(monkeypatch):
    monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://s3.example.com")
    provider = S3StorageProvider()
    provider._client = MagicMock()
    return provider


def test_s3_storage_save_calls_put_object(s3_provider):
    key = s3_provider.save("resumes/test.pdf", b"hello")

    assert key == "resumes/test.pdf"
    s3_provider._client.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="resumes/test.pdf", Body=b"hello"
    )


def test_s3_storage_save_raises_storage_error_on_failure(s3_provider):
    s3_provider._client.put_object.side_effect = _client_error("PutObject")

    with pytest.raises(StorageError):
        s3_provider.save("resumes/test.pdf", b"hello")


def test_s3_storage_delete_calls_delete_object(s3_provider):
    s3_provider.delete("resumes/test.pdf")

    s3_provider._client.delete_object.assert_called_once_with(Bucket="test-bucket", Key="resumes/test.pdf")


def test_s3_storage_delete_raises_on_a_real_failure(s3_provider):
    """Inverted 2026-10-19. This test used to be `..._does_not_raise_on_failure`.

    It asserted that `delete` swallowed a ClientError, which was the defect written down as
    intended behaviour. Because `delete` is typed `-> None`, a caller had no way to tell a
    deletion from a failure -- and `account_service.delete_account` relied on exactly that
    silence, committing the rows that held the only copy of the storage key while the object
    was still in the bucket. That produced an unreachable orphan belonging to a user who had
    been told their data was erased.

    `_client_error` raises code "500", a genuine server error, so this must raise.
    """
    s3_provider._client.delete_object.side_effect = _client_error("DeleteObject")

    with pytest.raises(StorageError):
        s3_provider.delete("resumes/test.pdf")


def test_s3_storage_delete_is_idempotent_for_a_key_that_is_already_gone(s3_provider):
    """The half of the old contract that was right, kept explicitly.

    An object that is already absent is success: deletion has to be idempotent, or a retry
    after a partial failure would fail on everything it had already removed.
    """
    s3_provider._client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "DeleteObject"
    )

    s3_provider.delete("resumes/test.pdf")


def test_s3_storage_load_returns_body_bytes(s3_provider):
    body = MagicMock()
    body.read.return_value = b"hello"
    s3_provider._client.get_object.return_value = {"Body": body}

    result = s3_provider.load("resumes/test.pdf")

    assert result == b"hello"
    s3_provider._client.get_object.assert_called_once_with(Bucket="test-bucket", Key="resumes/test.pdf")


def test_s3_storage_load_raises_storage_error_on_failure(s3_provider):
    s3_provider._client.get_object.side_effect = _client_error("GetObject")

    with pytest.raises(StorageError):
        s3_provider.load("resumes/test.pdf")


def test_every_concrete_provider_declares_its_name():
    """A provider without a name silently loses the wrong-backend protection.

    `deletion_worker.execute_job` compares the name a job was enqueued under with the name of
    the provider it was handed, and skips the comparison when the provider does not declare
    one -- which is right for a test double but would quietly disarm the check for a real
    adapter that forgot the attribute. This is the test that notices.
    """
    import inspect

    import app.storage.local_provider as local_mod
    import app.storage.s3_provider as s3_mod
    from app.storage.provider import StorageProvider

    found = {}
    for module in (local_mod, s3_mod):
        for _label, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, StorageProvider) and obj is not StorageProvider:
                found[obj.__name__] = getattr(obj, "name", None)

    assert found == {"LocalStorageProvider": "local", "S3StorageProvider": "s3"}, found
    assert all(isinstance(v, str) and v for v in found.values())


def test_s3_delete_failure_carries_the_providers_error_code(s3_provider):
    """`AccessDenied` will never succeed on its own; `SlowDown` will. The code is the only
    non-sensitive thing that separates them, and an operator triaging a stuck erasure has
    nothing else: every adapter wraps every failure in the one `StorageError` class, and the
    message is withheld because it quotes the bucket and the key."""
    s3_provider._client.delete_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}}, "DeleteObject"
    )

    with pytest.raises(StorageError) as caught:
        s3_provider.delete("resumes/test.pdf")

    assert caught.value.code == "AccessDenied"


def test_s3_transport_failure_carries_the_botocore_class_as_its_code(s3_provider):
    """A transport fault has no response and so no error code. The botocore class names the
    fault without quoting the endpoint."""
    from botocore.exceptions import EndpointConnectionError

    s3_provider._client.delete_object.side_effect = EndpointConnectionError(
        endpoint_url="https://s3.example.com"
    )

    with pytest.raises(StorageError) as caught:
        s3_provider.delete("resumes/test.pdf")

    assert caught.value.code == "EndpointConnectionError"


def test_local_delete_permission_failure_carries_the_errno_name(tmp_path, monkeypatch):
    """The local adapter's equivalent triage signal. EACCES needs a human; EBUSY may clear."""
    import errno
    from pathlib import Path

    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def _refuse(self, missing_ok=False):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(Path, "unlink", _refuse)

    with pytest.raises(StorageError) as caught:
        LocalStorageProvider().delete("resumes/test.pdf")

    assert caught.value.code == "EACCES"

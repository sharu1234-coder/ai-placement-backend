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


def test_s3_storage_delete_does_not_raise_on_failure(s3_provider):
    s3_provider._client.delete_object.side_effect = _client_error("DeleteObject")

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

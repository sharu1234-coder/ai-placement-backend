"""Erasure against a real S3-compatible service. Skipped unless a disposable bucket is given.

**This has never been executed.** No storage vendor has been chosen for this project, no
bucket exists, and no credential exists, so every assertion below is unproven against a real
service. It is written now because the unit tests cannot close this gap by construction: they
mock the boto3 client, so they prove the request shapes and the handling of every documented
response shape, and they cannot prove that AWS behaves as documented. Only this can.

Running it permanently deletes object versions, which cannot be undone -- there is no delete
marker to remove and no previous version to restore. So it refuses to run unless
`tests/s3_safety.py` is satisfied that the target is disposable: an explicit opt-in, a bucket
whose name says what it is and does not say "prod", a bucket that is not the application's
own, and a non-wildcard prefix ending in a separator. Absence of configuration is a skip;
wrong configuration is an error.

**Do not run this against a bucket the owner has not confirmed is disposable.** The guard
catches accidents, not intent.

What it covers, and why each one needs a real service:

* that a version-less delete really does leave the data behind, which is the premise of the
  whole stage and is the one thing a mock cannot establish;
* that deleting by version id really removes it from the version history;
* that a delete marker can be removed and is counted as a marker;
* that pagination continues correctly across real `NextKeyMarker` / `NextVersionIdMarker`
  values, which the service generates and a mock invents;
* that a neighbouring key sharing the prefix is untouched;
* that a second erasure adds nothing, so a retry is free.

Set-up it requires, which is deliberately not automated: a bucket created for this and nothing
else, versioning enabled on it, and a principal whose policy allows `s3:PutObject`,
`s3:GetObject`, `s3:DeleteObject`, `s3:DeleteObjectVersion`, `s3:ListBucket`,
`s3:ListBucketVersions` and `s3:GetBucketVersioning` on `<bucket>/<prefix>*` -- and
**not** `s3:BypassGovernanceRetention`, which nothing here uses and which would weaken what
the lock tests could prove.
"""

import uuid

import pytest

from app.core.config import settings
from app.storage import erasure
from app.storage.provider import StorageError
from app.storage.s3_provider import S3StorageProvider
from tests.s3_safety import resolve_disposable_bucket

# Import-time, so a misconfigured target stops collection rather than a test.
TARGET = resolve_disposable_bucket()

pytestmark = pytest.mark.skipif(
    TARGET is None,
    reason=(
        "no disposable S3 bucket is configured; set TEST_S3_BUCKET, TEST_S3_PREFIX and "
        "TEST_S3_DESTRUCTIVE=1 against a bucket created for this and nothing else"
    ),
)


@pytest.fixture()
def provider(monkeypatch):
    """A provider pointed at the disposable bucket, with its versioning verified.

    Credentials are left to boto3's own resolution chain, so this module never holds one.
    """
    monkeypatch.setattr(settings, "storage_s3_bucket", TARGET.bucket)
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", TARGET.endpoint)
    monkeypatch.setattr(settings, "storage_s3_region", TARGET.region)
    monkeypatch.setattr(settings, "storage_s3_expected_bucket_owner", TARGET.expected_owner)
    storage = S3StorageProvider()

    status = storage.versioning_status()
    if status != erasure.VERSIONING_ENABLED:
        # Not a skip: the bucket is configured as destructive and the precondition for the
        # thing being tested is absent, which is a set-up fault to fix rather than a condition
        # to tiptoe around.
        raise AssertionError(
            f"{TARGET.describe()} reports versioning {status!r}. These tests exist to prove "
            "behaviour on a versioned bucket, so enable versioning on the disposable bucket "
            "before running them."
        )
    return storage


@pytest.fixture()
def key(provider):
    """A unique key inside the isolated prefix, cleaned up with raw client calls.

    The teardown deliberately does not use `erase`: cleaning up with the code under test would
    make a failure of that code look like a clean run.
    """
    name = TARGET.key(f"{uuid.uuid4().hex}.bin")
    assert name.startswith(TARGET.prefix), "every key must stay inside the isolated prefix"
    yield name
    _purge(provider, name)


def _purge(storage, key_name):
    """Remove every version of a key, by id, without going through erase()."""
    client = storage._client
    args = {"Bucket": TARGET.bucket, "Prefix": key_name}
    if TARGET.expected_owner:
        args["ExpectedBucketOwner"] = TARGET.expected_owner
    while True:
        page = client.list_object_versions(**args)
        entries = [
            {"Key": entry["Key"], "VersionId": entry["VersionId"]}
            for group in ("Versions", "DeleteMarkers")
            for entry in page.get(group) or ()
            if entry.get("Key") == key_name
        ]
        if entries:
            delete_args = {"Bucket": TARGET.bucket, "Delete": {"Objects": entries}}
            if TARGET.expected_owner:
                delete_args["ExpectedBucketOwner"] = TARGET.expected_owner
            client.delete_objects(**delete_args)
        if not page.get("IsTruncated"):
            return
        args["KeyMarker"] = page["NextKeyMarker"]
        if page.get("NextVersionIdMarker"):
            args["VersionIdMarker"] = page["NextVersionIdMarker"]


def test_a_version_less_delete_leaves_the_data_behind(provider, key):
    """The premise of the entire stage, confirmed against the service rather than quoted.

    If this ever fails, the version-aware erasure is unnecessary -- so it is worth knowing.
    """
    provider.save(key, b"first")
    provider.delete(key)

    history = provider.inventory(key)

    assert any(not entry.is_delete_marker for entry in history), (
        "a version-less delete should leave the object version in place"
    )
    assert any(entry.is_delete_marker for entry in history), (
        "a version-less delete should insert a delete marker"
    )
    with pytest.raises(StorageError):
        # And the data is unreachable through the ordinary read path while still being stored,
        # which is what makes a 404 worthless as evidence.
        provider.load(key)


def test_every_version_is_erased(provider, key):
    provider.save(key, b"v1")
    provider.save(key, b"v2")
    provider.save(key, b"v3")
    assert len(provider.inventory(key)) == 3

    outcome = provider.erase(key)

    assert provider.inventory(key) == []
    assert outcome.versions_deleted == 3
    assert outcome.markers_deleted == 0
    assert outcome.verified_by == erasure.VERIFIED_BY_INVENTORY


def test_a_delete_marker_is_erased_too(provider, key):
    """A marker left behind keeps the key listed as deleted with live versions underneath."""
    provider.save(key, b"v1")
    provider.delete(key)

    outcome = provider.erase(key)

    assert provider.inventory(key) == []
    assert outcome.markers_deleted == 1
    assert outcome.versions_deleted == 1


def test_a_second_erasure_adds_nothing(provider, key):
    """What a version-less delete cannot do: repeating it adds a marker every time."""
    provider.save(key, b"v1")
    provider.erase(key)

    second = provider.erase(key)

    assert second.already_absent is True
    assert second.removed == 0
    assert provider.inventory(key) == []


def test_pagination_continues_across_real_markers(provider, key):
    """Real continuation tokens, which a mock can only invent.

    The page size is lowered on the instance rather than writing a thousand versions: the
    behaviour under test is whether `NextKeyMarker` and `NextVersionIdMarker` are fed back
    correctly, and that happens at any page size.
    """
    for index in range(5):
        provider.save(key, f"v{index}".encode())
    provider._LIST_PAGE = 2

    history = provider.inventory(key)

    assert len(history) == 5, "pagination must return every version, not just the first page"
    assert len({entry.version_id for entry in history}) == 5, "no version may be returned twice"
    assert provider.erase(key).versions_deleted == 5
    assert provider.inventory(key) == []


def test_a_neighbouring_key_is_untouched(provider, key):
    """`Prefix` matches prefixes, so the neighbour is returned by the same listing."""
    neighbour = f"{key}.bak"
    provider.save(key, b"mine")
    provider.save(neighbour, b"theirs")
    try:
        provider.erase(key)

        assert provider.inventory(key) == []
        assert len(provider.inventory(neighbour)) == 1
        assert provider.load(neighbour) == b"theirs"
    finally:
        _purge(provider, neighbour)


def test_an_absent_key_is_an_idempotent_success(provider):
    absent = TARGET.key(f"never-written-{uuid.uuid4().hex}.bin")

    outcome = provider.erase(absent)

    assert outcome.already_absent is True
    assert outcome.verified_by == erasure.VERIFIED_BY_INVENTORY


def test_the_owner_guard_is_accepted_by_the_service(provider):
    """Only meaningful when an owner is configured: it proves the guard is sent in a form the
    service accepts, so a 403 later really would mean a mismatch rather than a malformed call.
    """
    if not TARGET.expected_owner:
        pytest.skip("no TEST_S3_EXPECTED_BUCKET_OWNER configured, so there is no guard to send")

    assert provider.versioning_status() == erasure.VERSIONING_ENABLED

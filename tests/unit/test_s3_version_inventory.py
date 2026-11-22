"""Enumerating what is actually stored under a key, which is the only evidence of an erasure.

On a versioned bucket, HEAD and GET answer 404 as soon as a delete marker exists -- and that
is exactly the state in which nothing has been erased. So "is it gone" has to be answered by
listing the key version history, and these tests are about the ways that listing can lie or be
read wrongly:

* a prefix is not a key, so a neighbouring key must not be mistaken for this one;
* a truncated listing needs two markers to continue, not one;
* a listing that cannot be continued must raise, because a partial enumeration that returned
  quietly would read as "nothing left".

Mocked at the boto3 client boundary. These tests prove the request shapes and the response
handling, not that AWS behaves as documented -- that needs the guarded integration test, which
has no bucket to run against yet. Nothing here touches a real bucket.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import settings
from app.storage import erasure
from app.storage.erasure import StoredVersion
from app.storage.provider import StorageError
from app.storage.s3_provider import S3StorageProvider

KEY = "resumes/42/cv.pdf"


@pytest.fixture()
def s3(monkeypatch):
    monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://s3.example.com")
    monkeypatch.setattr(settings, "storage_s3_expected_bucket_owner", None)
    provider = S3StorageProvider()
    provider._client = MagicMock()
    return provider


@pytest.fixture()
def guarded(monkeypatch):
    monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
    monkeypatch.setattr(settings, "storage_s3_endpoint_url", "https://s3.example.com")
    monkeypatch.setattr(settings, "storage_s3_expected_bucket_owner", "111111111111")
    provider = S3StorageProvider()
    provider._client = MagicMock()
    return provider


def _error(code: str, operation: str = "ListObjectVersions") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


def _page(versions=(), markers=(), truncated=False, next_key=None, next_version=None):
    page = {
        "Versions": [dict(Key=k, VersionId=v) for k, v in versions],
        "DeleteMarkers": [dict(Key=k, VersionId=v) for k, v in markers],
        "IsTruncated": truncated,
    }
    if next_key is not None:
        page["NextKeyMarker"] = next_key
    if next_version is not None:
        page["NextVersionIdMarker"] = next_version
    return page


class TestTheBucketVersioningModeIsAsked:
    """Every later decision rests on this answer, so it is asked rather than assumed."""

    @pytest.mark.parametrize(
        "status, expected",
        [
            ("Enabled", erasure.VERSIONING_ENABLED),
            ("Suspended", erasure.VERSIONING_SUSPENDED),
        ],
    )
    def test_the_two_versioned_modes_are_reported(self, s3, status, expected):
        s3._client.get_bucket_versioning.return_value = {"Status": status}

        assert s3.versioning_status() == expected

    def test_a_bucket_that_never_had_versioning_reports_no_versioning(self, s3):
        """GetBucketVersioning returns no Status member at all for such a bucket, which is the
        one case where a version-less delete really is permanent."""
        s3._client.get_bucket_versioning.return_value = {}

        assert s3.versioning_status() == erasure.VERSIONING_NONE

    def test_an_unrecognised_status_is_refused_rather_than_defaulted(self, s3):
        """An unknown versioning mode is an unknown deletion semantic. Defaulting it either way
        risks leaving the data in place while reporting an erasure."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Paused"}

        with pytest.raises(StorageError) as caught:
            s3.versioning_status()

        assert caught.value.code == erasure.ERASURE_VERSIONING_UNKNOWN

    def test_a_refused_probe_keeps_the_service_error_code(self, s3):
        """`AccessDenied` here is a permission to grant; it must not be flattened into a
        generic failure, because that is the difference between a fix and a guess."""
        s3._client.get_bucket_versioning.side_effect = _error(
            "AccessDenied", "GetBucketVersioning"
        )

        with pytest.raises(StorageError) as caught:
            s3.versioning_status()

        assert caught.value.code == "AccessDenied"

    def test_a_missing_bucket_is_reported_as_such(self, s3):
        """Never an erasure. The bucket not existing says nothing about the object."""
        s3._client.get_bucket_versioning.side_effect = _error(
            "NoSuchBucket", "GetBucketVersioning"
        )

        with pytest.raises(StorageError) as caught:
            s3.versioning_status()

        assert caught.value.code == "NoSuchBucket"

    def test_a_transport_failure_carries_the_botocore_class(self, s3):
        s3._client.get_bucket_versioning.side_effect = EndpointConnectionError(
            endpoint_url="https://s3.example.com"
        )

        with pytest.raises(StorageError) as caught:
            s3.versioning_status()

        assert caught.value.code == "EndpointConnectionError"

    def test_the_message_does_not_name_the_bucket(self, s3):
        s3._client.get_bucket_versioning.return_value = {"Status": "Paused"}

        with pytest.raises(StorageError) as caught:
            s3.versioning_status()

        assert "test-bucket" not in str(caught.value)


class TestTheInventoryReadsTheWholeHistory:
    def test_versions_and_delete_markers_are_both_collected(self, s3):
        """Both have to go. A version left behind keeps the data; a delete marker left behind
        keeps the key looking deleted while an older version sits underneath it."""
        s3._client.list_object_versions.return_value = _page(
            versions=[(KEY, "v2"), (KEY, "v1")], markers=[(KEY, "m1")]
        )

        assert s3.inventory(KEY) == [
            StoredVersion("v2"),
            StoredVersion("v1"),
            StoredVersion("m1", is_delete_marker=True),
        ]

    def test_an_empty_history_is_an_empty_list(self, s3):
        s3._client.list_object_versions.return_value = _page()

        assert s3.inventory(KEY) == []

    def test_a_neighbouring_key_is_never_included(self, s3):
        """`Prefix` selects "those keys that begin with the specified prefix", so asking for
        this key also returns its neighbours. Including one would delete another object
        version; counting one as a survivor would strand a finished job for ever."""
        s3._client.list_object_versions.return_value = _page(
            versions=[(KEY, "mine"), (KEY + ".bak", "theirs"), (KEY + "/old", "theirs2")],
            markers=[(KEY + ".bak", "theirmarker")],
        )

        assert s3.inventory(KEY) == [StoredVersion("mine")]

    def test_an_entry_with_no_version_id_is_skipped(self, s3):
        """There is nothing to delete without one, and passing None as a VersionId would send a
        version-less delete -- which on a versioned bucket inserts a marker instead."""
        s3._client.list_object_versions.return_value = _page(versions=[(KEY, None), (KEY, "v1")])

        assert s3.inventory(KEY) == [StoredVersion("v1")]

    def test_the_request_asks_only_about_this_key(self, s3):
        s3._client.list_object_versions.return_value = _page()

        s3.inventory(KEY)

        s3._client.list_object_versions.assert_called_once_with(
            Bucket="test-bucket", Prefix=KEY, MaxKeys=1000
        )

    def test_the_first_page_sends_no_markers(self, s3):
        """A VersionIdMarker without a KeyMarker has nothing to anchor to."""
        s3._client.list_object_versions.return_value = _page()

        s3.inventory(KEY)

        sent = s3._client.list_object_versions.call_args.kwargs
        assert "KeyMarker" not in sent and "VersionIdMarker" not in sent


class TestPagination:
    def test_both_markers_continue_a_truncated_listing(self, s3):
        """One key can have more versions than fit in a page, so the key marker alone would
        restart that key from the top and the listing would never end."""
        s3._client.list_object_versions.side_effect = [
            _page(versions=[(KEY, "v3")], truncated=True, next_key=KEY, next_version="v3"),
            _page(versions=[(KEY, "v2")], truncated=True, next_key=KEY, next_version="v2"),
            _page(versions=[(KEY, "v1")], markers=[(KEY, "m1")]),
        ]

        assert s3.inventory(KEY) == [
            StoredVersion("v3"),
            StoredVersion("v2"),
            StoredVersion("v1"),
            StoredVersion("m1", is_delete_marker=True),
        ]
        second = s3._client.list_object_versions.call_args_list[1].kwargs
        assert second["KeyMarker"] == KEY
        assert second["VersionIdMarker"] == "v3"

    def test_a_key_marker_alone_is_accepted_when_the_service_sends_no_version_marker(self, s3):
        """The version marker is documented as the first version not returned, and a service
        may omit it at a key boundary. The key marker alone is then enough to continue."""
        s3._client.list_object_versions.side_effect = [
            _page(versions=[(KEY, "v2")], truncated=True, next_key=KEY),
            _page(versions=[(KEY, "v1")]),
        ]

        assert s3.inventory(KEY) == [StoredVersion("v2"), StoredVersion("v1")]
        second = s3._client.list_object_versions.call_args_list[1].kwargs
        assert second["KeyMarker"] == KEY
        assert "VersionIdMarker" not in second

    def test_a_truncated_page_with_no_key_marker_raises(self, s3):
        """The listing cannot be continued, so what came back is a fragment. Returning it
        quietly would read as "nothing left" over data that is still there."""
        s3._client.list_object_versions.return_value = _page(
            versions=[(KEY, "v1")], truncated=True
        )

        with pytest.raises(StorageError) as caught:
            s3.inventory(KEY)

        assert caught.value.code == erasure.ERASURE_INVENTORY_BROKEN

    def test_markers_that_stop_advancing_raise_instead_of_looping(self, s3):
        """A service that repeats its own marker would otherwise spin this worker for ever."""
        repeated = _page(versions=[(KEY, "v1")], truncated=True, next_key=KEY, next_version="v1")
        s3._client.list_object_versions.side_effect = [repeated, repeated, repeated]

        with pytest.raises(StorageError) as caught:
            s3.inventory(KEY)

        assert caught.value.code == erasure.ERASURE_INVENTORY_BROKEN
        assert s3._client.list_object_versions.call_count == 2, "it must stop, not keep asking"


class TestWhenTheServiceWillNotEnumerate:
    @pytest.mark.parametrize("code", ["NotImplemented", "NotSupported", "MethodNotAllowed"])
    def test_a_service_without_version_listing_is_reported_as_unprovable(self, s3, code):
        """Some S3-compatible services do not implement it. That is not an erasure and not a
        transient fault: there is no way to establish what remains, so it needs a human."""
        s3._client.list_object_versions.side_effect = _error(code)

        with pytest.raises(StorageError) as caught:
            s3.inventory(KEY)

        assert caught.value.code == erasure.ERASURE_INVENTORY_UNSUPPORTED

    def test_a_denied_listing_keeps_its_own_code(self, s3):
        """`AccessDenied` on the listing is a missing `s3:ListBucketVersions` permission, which
        an operator fixes. Calling it "unsupported" would send them looking for the wrong thing."""
        s3._client.list_object_versions.side_effect = _error("AccessDenied")

        with pytest.raises(StorageError) as caught:
            s3.inventory(KEY)

        assert caught.value.code == "AccessDenied"

    def test_a_transport_failure_carries_the_botocore_class(self, s3):
        s3._client.list_object_versions.side_effect = EndpointConnectionError(
            endpoint_url="https://s3.example.com"
        )

        with pytest.raises(StorageError) as caught:
            s3.inventory(KEY)

        assert caught.value.code == "EndpointConnectionError"


class TestTheOwnerGuardIsAsserted:
    """`ExpectedBucketOwner` puts the ownership check in the service, which answers 403, rather
    than in this process comparing its own configuration with itself."""

    def test_every_call_carries_it_when_configured(self, guarded):
        guarded._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        guarded._client.list_object_versions.return_value = _page()
        guarded._client.get_object.return_value = {"Body": MagicMock(read=lambda: b"x")}

        guarded.versioning_status()
        guarded.inventory(KEY)
        guarded.save(KEY, b"x")
        guarded.delete(KEY)
        guarded.load(KEY)

        for call in (
            guarded._client.get_bucket_versioning.call_args,
            guarded._client.list_object_versions.call_args,
            guarded._client.put_object.call_args,
            guarded._client.delete_object.call_args,
            guarded._client.get_object.call_args,
        ):
            assert call.kwargs.get("ExpectedBucketOwner") == "111111111111"

    def test_no_call_carries_it_when_it_is_not_configured(self, s3):
        """A deployment without the setting must send exactly the request it sent before this
        existed, so introducing the guard cannot change behaviour anywhere it is unset."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = _page()

        s3.versioning_status()
        s3.inventory(KEY)
        s3.delete(KEY)

        for call in (
            s3._client.get_bucket_versioning.call_args,
            s3._client.list_object_versions.call_args,
            s3._client.delete_object.call_args,
        ):
            assert "ExpectedBucketOwner" not in call.kwargs

    def test_the_identity_reports_the_guard_without_a_credential(self, guarded):
        identity = guarded.identity()

        assert identity.expected_owner == "111111111111"
        assert "111111111111" not in identity.describe()
        assert not any("key" in field.lower() for field in vars(identity))

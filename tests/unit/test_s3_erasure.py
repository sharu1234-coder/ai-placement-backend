"""Permanently erasing an object, which is not what deleting one does.

AWS is explicit about the defect this closes: "When versioning is enabled, a simple DELETE
cannot permanently delete an object. (A simple DELETE request is a request that doesn't specify
a version ID.) Instead, Amazon S3 inserts a delete marker". The object stops being listed, the
API answers 404, every byte is still stored under its version id -- and the deletion job is
marked done, the request closes, the last row holding the key is deleted. The data outlives a
deletion the user was told was permanent.

So the assertions here are mostly about three things:

* every delete carries an explicit `VersionId`, because one without is the defect;
* the end state is established by enumerating the version history, never by a 404;
* nothing is reported as erased that was not enumerated as gone afterwards.

Plus two that are about refusing rather than doing: governance bypass is never sent, and a
protected version is reported as protected rather than worked around.

Mocked at the boto3 client boundary. This proves the request shapes and the handling of every
documented response shape; it does not prove AWS behaves as documented. That needs the guarded
integration test in `tests/integration/`, which has no bucket to run against yet. Nothing here
touches a real bucket.
"""

import datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import settings
from app.storage import erasure
from app.storage.local_provider import LocalStorageProvider
from app.storage.provider import StorageError, StorageProvider
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


def _error(code: str, operation: str = "DeleteObjects") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "no"}}, operation)


def _versioned(s3, status="Enabled", versions=(), markers=()):
    """Point the mock at a bucket with this versioning mode and this history."""
    s3._client.get_bucket_versioning.return_value = {"Status": status}
    s3._client.list_object_versions.return_value = {
        "Versions": [{"Key": KEY, "VersionId": v} for v in versions],
        "DeleteMarkers": [{"Key": KEY, "VersionId": v} for v in markers],
        "IsTruncated": False,
    }


def _echo_deletes(errors=()):
    """A DeleteObjects that succeeds for everything asked, apart from the given version ids."""
    failed = {version_id: code for code, version_id in errors}

    def respond(**kwargs):
        asked = kwargs["Delete"]["Objects"]
        return {
            "Deleted": [o for o in asked if o["VersionId"] not in failed],
            "Errors": [
                {"Key": o["Key"], "VersionId": o["VersionId"], "Code": failed[o["VersionId"]]}
                for o in asked
                if o["VersionId"] in failed
            ],
        }

    return respond


def _emptied_after_delete(s3, before_versions, before_markers=()):
    """History as given on the first listing, empty on every listing after the delete."""
    empty = {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}
    s3._client.list_object_versions.side_effect = [
        {
            "Versions": [{"Key": KEY, "VersionId": v} for v in before_versions],
            "DeleteMarkers": [{"Key": KEY, "VersionId": v} for v in before_markers],
            "IsTruncated": False,
        },
        empty,
    ]


class TestTheContract:
    def test_a_provider_without_version_aware_erasure_refuses(self):
        """Fail closed. The alternative -- falling back to a plain delete -- is the defect."""

        class Bare(StorageProvider):
            name = "bare"

            def save(self, key, data):
                return key

            def delete(self, key):
                pass

            def load(self, key):
                return b""

        with pytest.raises(StorageError) as caught:
            Bare().erase(KEY)

        assert caught.value.code == erasure.ERASURE_NOT_IMPLEMENTED

    def test_every_concrete_adapter_implements_it(self):
        """The default refusal is there for test doubles, not for real adapters. An adapter
        that inherited it would have every account deletion held for an operator for ever."""
        import inspect

        import app.storage.local_provider as local_mod
        import app.storage.s3_provider as s3_mod

        for module in (local_mod, s3_mod):
            for _label, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, StorageProvider) and obj is not StorageProvider:
                    assert "erase" in vars(obj), f"{obj.__name__} does not implement erase()"


class TestAVersionedBucket:
    def test_every_version_and_marker_is_deleted_by_id(self, s3):
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v3", "v2", "v1"], ["m1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        outcome = s3.erase(KEY)

        sent = s3._client.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert sent == [
            {"Key": KEY, "VersionId": "v3"},
            {"Key": KEY, "VersionId": "v2"},
            {"Key": KEY, "VersionId": "v1"},
            {"Key": KEY, "VersionId": "m1"},
        ]
        assert outcome.versions_deleted == 3
        assert outcome.markers_deleted == 1
        assert outcome.versioning == erasure.VERSIONING_ENABLED
        assert outcome.verified_by == erasure.VERIFIED_BY_INVENTORY

    def test_a_version_less_delete_is_never_sent(self, s3):
        """The one call that would insert a delete marker instead of erasing anything."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        s3.erase(KEY)

        s3._client.delete_object.assert_not_called()
        for entry in s3._client.delete_objects.call_args.kwargs["Delete"]["Objects"]:
            assert entry["VersionId"], "a delete with no version id erases nothing"

    def test_governance_bypass_is_never_requested(self, s3):
        """It would make most locks deletable, which turns a retention policy into a
        suggestion. Asserted on the request rather than trusted to code review."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        s3.erase(KEY)

        assert "BypassGovernanceRetention" not in s3._client.delete_objects.call_args.kwargs

    def test_successes_are_reported_rather_than_assumed(self, s3):
        """Quiet mode "includes only keys where the delete operation encountered an error", so
        the request has to ask for the successes the counts are made of."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        s3.erase(KEY)

        assert s3._client.delete_objects.call_args.kwargs["Delete"]["Quiet"] is False

    def test_the_history_is_enumerated_again_after_deleting(self, s3):
        """A multi-object delete can answer 200 OK with failures inside it, so the call not
        raising proves nothing. Only an empty listing afterwards does."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        s3.erase(KEY)

        assert s3._client.list_object_versions.call_count == 2


class TestASuspendedBucket:
    def test_the_null_version_and_the_older_history_are_both_erased(self, s3):
        """Suspension does not remove the history written before it. AWS on a version-less
        delete here: it "Can only remove an object whose version ID is null", and "you will
        lose the content for the null version when a delete marker replaces it" -- one copy
        destroyed, the rest hidden. Deleting by id erases all of them instead."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        _emptied_after_delete(s3, ["null", "v1"], ["m1"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        outcome = s3.erase(KEY)

        sent = [o["VersionId"] for o in s3._client.delete_objects.call_args.kwargs["Delete"]["Objects"]]
        assert sent == ["null", "v1", "m1"]
        assert outcome.versioning == erasure.VERSIONING_SUSPENDED
        assert outcome.removed == 3

    def test_the_null_version_is_addressed_as_a_version(self, s3):
        """"null" is a version id like any other once it has been listed."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        _emptied_after_delete(s3, ["null"])
        s3._client.delete_objects.side_effect = _echo_deletes()

        outcome = s3.erase(KEY)

        assert outcome.versions_deleted == 1
        s3._client.delete_object.assert_not_called()


class TestAnUnversionedBucket:
    def test_a_plain_delete_is_used_and_confirmed(self, s3):
        """No history exists to enumerate, so a version-less delete really is permanent here --
        and that is established from GetBucketVersioning, not assumed."""
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.side_effect = [{}, _error("404", "HeadObject")]

        outcome = s3.erase(KEY)

        s3._client.delete_object.assert_called_once_with(Bucket="test-bucket", Key=KEY)
        s3._client.delete_objects.assert_not_called()
        assert outcome.versioning == erasure.VERSIONING_NONE
        assert outcome.verified_by == erasure.VERIFIED_BY_ABSENCE_WITHOUT_HISTORY
        assert outcome.versions_deleted == 1

    def test_an_object_still_there_afterwards_is_not_an_erasure(self, s3):
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.return_value = {}

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_VERSIONS_REMAIN

    def test_an_absent_object_is_an_idempotent_success(self, s3):
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.side_effect = _error("404", "HeadObject")

        outcome = s3.erase(KEY)

        assert outcome.already_absent is True
        assert outcome.removed == 0
        s3._client.delete_object.assert_not_called()

    def test_an_unreadable_head_is_not_read_as_absence(self, s3):
        """`AccessDenied` on HEAD means "cannot tell", which must never become "it is gone"."""
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.side_effect = _error("AccessDenied", "HeadObject")

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "AccessDenied"

    def test_the_evidence_is_labelled_as_the_weaker_kind(self, s3):
        """A 404 is only evidence because the service said there is no history to hide behind
        it. Recording which evidence was used is what keeps the two from looking alike."""
        s3._client.get_bucket_versioning.return_value = {}
        s3._client.head_object.side_effect = [{}, _error("404", "HeadObject")]

        assert s3.erase(KEY).verified_by != erasure.VERIFIED_BY_INVENTORY


class TestAlreadyAbsentVersions:
    def test_an_empty_history_erases_nothing_and_succeeds(self, s3):
        _versioned(s3, versions=[], markers=[])

        outcome = s3.erase(KEY)

        assert outcome.already_absent is True
        s3._client.delete_objects.assert_not_called()
        s3._client.delete_object.assert_not_called()

    def test_absence_is_established_by_listing_and_not_by_a_404(self, s3):
        """The distinction the whole stage rests on: HEAD answers 404 while every version is
        still stored, so the question is never asked that way on a versioned bucket."""
        _versioned(s3, versions=[], markers=[])

        s3.erase(KEY)

        s3._client.head_object.assert_not_called()
        assert s3._client.list_object_versions.called


class TestRepeatedExecution:
    def test_a_second_run_adds_no_delete_marker(self, s3):
        """The requirement that a version-less delete cannot satisfy: repeating that call adds
        another marker every time. Deleting by id and then finding the history empty is what
        makes a retry free."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        empty = {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}
        s3._client.list_object_versions.side_effect = [
            {"Versions": [{"Key": KEY, "VersionId": "v1"}], "DeleteMarkers": [], "IsTruncated": False},
            empty,
            empty,
        ]
        s3._client.delete_objects.side_effect = _echo_deletes()

        first = s3.erase(KEY)
        second = s3.erase(KEY)

        assert first.versions_deleted == 1
        assert second.already_absent is True
        assert s3._client.delete_objects.call_count == 1, "the retry must send no delete at all"
        s3._client.delete_object.assert_not_called()

    def test_a_retry_after_a_partial_failure_finishes_the_job(self, s3):
        """First pass loses one version to a transient fault; the second erases what is left."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        both = {
            "Versions": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }
        left = {"Versions": [{"Key": KEY, "VersionId": "v2"}], "DeleteMarkers": [], "IsTruncated": False}
        empty = {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}
        s3._client.list_object_versions.side_effect = [both, left, left, empty]
        s3._client.delete_objects.side_effect = [
            _echo_deletes(errors=[("InternalError", "v2")])(
                Delete={"Objects": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}]}
            ),
            _echo_deletes()(Delete={"Objects": [{"Key": KEY, "VersionId": "v2"}]}),
        ]

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)
        assert caught.value.code == "InternalError"

        outcome = s3.erase(KEY)
        assert outcome.versions_deleted == 1
        assert outcome.already_absent is False


class TestDeleteBatching:
    def test_versions_are_deleted_in_batches_of_a_thousand(self, s3):
        """AWS documents the cap: a request can contain a list of up to 1,000 keys to delete."""
        many = [f"v{index}" for index in range(2500)]
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, many)
        s3._client.delete_objects.side_effect = _echo_deletes()

        outcome = s3.erase(KEY)

        sizes = [
            len(call.kwargs["Delete"]["Objects"])
            for call in s3._client.delete_objects.call_args_list
        ]
        assert sizes == [1000, 1000, 500]
        assert outcome.versions_deleted == 2500

    def test_every_batch_carries_the_owner_guard(self, monkeypatch):
        monkeypatch.setattr(settings, "storage_s3_bucket", "test-bucket")
        monkeypatch.setattr(settings, "storage_s3_endpoint_url", None)
        monkeypatch.setattr(settings, "storage_s3_expected_bucket_owner", "111111111111")
        s3 = S3StorageProvider()
        s3._client = MagicMock()
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, [f"v{index}" for index in range(1200)])
        s3._client.delete_objects.side_effect = _echo_deletes()

        s3.erase(KEY)

        for call in s3._client.delete_objects.call_args_list:
            assert call.kwargs["ExpectedBucketOwner"] == "111111111111"


class TestPartialFailures:
    def test_a_surviving_version_is_never_reported_as_erased(self, s3):
        """The headline rule: a job may not be marked done while a version remains."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("InternalError", "v2")])

        with pytest.raises(StorageError):
            s3.erase(KEY)

    def test_the_reported_failure_explains_the_survivors(self, s3):
        """A per-object code names what to fix; "versions remain" only says it did not work."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v1"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.side_effect = _error("InvalidRequest", "GetObjectLegalHold")
        s3._client.get_object_retention.side_effect = _error("InvalidRequest", "GetObjectRetention")

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "AccessDenied"

    def test_versions_that_survive_with_no_reported_failure_still_raise(self, s3):
        """Every delete reported success and the listing disagrees. The listing wins."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v1"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }
        s3._client.delete_objects.side_effect = _echo_deletes()

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_VERSIONS_REMAIN

    def test_a_reported_failure_that_left_nothing_behind_is_not_an_error(self, s3):
        """A concurrent worker finishing the same job looks exactly like this. The obligation
        is that no version remains, not that this process removed it."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        _emptied_after_delete(s3, ["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("InternalError", "v1")])

        outcome = s3.erase(KEY)

        assert outcome.verified_by == erasure.VERIFIED_BY_INVENTORY

    def test_a_permanent_failure_is_reported_in_preference_to_a_transient_one(self, s3):
        """An operator needs the reason that will not clear on its own."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.return_value = {
            "Versions": [{"Key": KEY, "VersionId": "v1"}, {"Key": KEY, "VersionId": "v2"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }
        s3._client.delete_objects.side_effect = _echo_deletes(
            errors=[("SlowDown", "v1"), ("MethodNotAllowed", "v2")]
        )

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "MethodNotAllowed"

    def test_a_whole_request_failure_keeps_the_service_code(self, s3):
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _error("NoSuchBucket")

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "NoSuchBucket"

    def test_a_transport_failure_keeps_the_botocore_class(self, s3):
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = EndpointConnectionError(
            endpoint_url="https://s3.example.com"
        )

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "EndpointConnectionError"


class TestProtectedVersions:
    """A lock is to be honoured, not worked around. AWS does not document which per-object code
    a blocked delete returns, so the code is not assumed -- the service is asked what is on the
    version that failed, and a lock is only reported when it says there is one."""

    def test_a_legal_hold_is_named_as_a_legal_hold(self, s3):
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.return_value = {"LegalHold": {"Status": "ON"}}

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_LEGAL_HOLD

    def test_a_live_retention_is_named_as_an_object_lock(self, s3):
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.return_value = {"LegalHold": {"Status": "OFF"}}
        s3._client.get_object_retention.return_value = {
            "Retention": {
                "Mode": "COMPLIANCE",
                "RetainUntilDate": datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(days=30),
            }
        }

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_OBJECT_LOCKED

    def test_an_expired_retention_is_not_blamed(self, s3):
        """It no longer blocks anything, so it cannot be what refused the delete. Reporting it
        would send an operator to wait out a date that has already passed."""
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.return_value = {"LegalHold": {"Status": "OFF"}}
        s3._client.get_object_retention.return_value = {
            "Retention": {
                "Mode": "GOVERNANCE",
                "RetainUntilDate": datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=1),
            }
        }

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "AccessDenied"

    def test_a_bucket_without_object_lock_falls_back_to_the_real_code(self, s3):
        """These calls fail outright on a bucket with no Object Lock configuration. That says
        nothing about the delete, so it must not replace the error that actually happened."""
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.side_effect = _error(
            "InvalidRequest", "GetObjectLegalHold"
        )
        s3._client.get_object_retention.side_effect = _error(
            "InvalidRequest", "GetObjectRetention"
        )

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "AccessDenied"

    def test_no_bypass_is_attempted_on_a_locked_version(self, s3):
        _versioned(s3, versions=["v1"])
        s3._client.delete_objects.side_effect = _echo_deletes(errors=[("AccessDenied", "v1")])
        s3._client.get_object_legal_hold.return_value = {"LegalHold": {"Status": "ON"}}

        with pytest.raises(StorageError):
            s3.erase(KEY)

        assert s3._client.delete_objects.call_count == 1, "no retry with a bypass"
        for call in s3._client.delete_objects.call_args_list:
            assert "BypassGovernanceRetention" not in call.kwargs


class TestTheVersioningProbeGatesEverything:
    def test_a_missing_bucket_stops_the_erasure_before_anything_is_deleted(self, s3):
        s3._client.get_bucket_versioning.side_effect = _error(
            "NoSuchBucket", "GetBucketVersioning"
        )

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == "NoSuchBucket"
        s3._client.delete_objects.assert_not_called()
        s3._client.delete_object.assert_not_called()

    def test_an_unknown_versioning_mode_deletes_nothing(self, s3):
        s3._client.get_bucket_versioning.return_value = {"Status": "Paused"}

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_VERSIONING_UNKNOWN
        s3._client.delete_objects.assert_not_called()
        s3._client.delete_object.assert_not_called()

    def test_a_service_that_will_not_list_versions_deletes_nothing(self, s3):
        """Versioned, but the history cannot be read, so no erasure can be proved. Fail closed
        rather than fall back to the delete that would only add a marker."""
        s3._client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        s3._client.list_object_versions.side_effect = _error(
            "NotImplemented", "ListObjectVersions"
        )

        with pytest.raises(StorageError) as caught:
            s3.erase(KEY)

        assert caught.value.code == erasure.ERASURE_INVENTORY_UNSUPPORTED
        s3._client.delete_objects.assert_not_called()
        s3._client.delete_object.assert_not_called()


class TestTheLocalAdapter:
    def test_it_deletes_and_confirms(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        storage = LocalStorageProvider()
        storage.save(KEY, b"cv")

        outcome = storage.erase(KEY)

        assert not (tmp_path / KEY).exists()
        assert outcome.versions_deleted == 1
        assert outcome.already_absent is False
        assert outcome.versioning == erasure.VERSIONING_NONE

    def test_an_absent_file_is_an_idempotent_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

        outcome = LocalStorageProvider().erase(KEY)

        assert outcome.already_absent is True
        assert outcome.removed == 0

    def test_its_evidence_is_labelled_as_absence_not_enumeration(self, tmp_path, monkeypatch):
        """A filesystem has no version history, which is why absence is enough here and is
        never enough on a versioned bucket."""
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

        outcome = LocalStorageProvider().erase(KEY)

        assert outcome.verified_by == erasure.VERIFIED_BY_ABSENCE_WITHOUT_HISTORY

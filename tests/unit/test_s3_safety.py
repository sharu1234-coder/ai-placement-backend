"""The gate between a typo and an irreversible deletion.

`tests/s3_safety.py` decides whether the erasure integration test may permanently delete object
versions in a bucket. There is no undo for that: a deleted version leaves no delete marker to
remove and no previous version to restore, so unlike the database guard there is not even a
backup to fall back on. That makes the guard worth testing as carefully as the code it guards,
which is the same reasoning behind `tests/unit/test_db_safety.py`.

Every test here is pure. Nothing connects to anything, and no bucket is needed -- the guard is
deliberately a parser, so it can be examined without the thing it protects.
"""

import dataclasses

import pytest

from tests.s3_safety import DisposableBucket, UnsafeTestBucket, check

SAFE = {
    "TEST_S3_BUCKET": "talviora-erasure-test",
    "TEST_S3_PREFIX": "ci-erasure-checks/",
    "TEST_S3_DESTRUCTIVE": "1",
}


def _env(**overrides):
    env = dict(SAFE)
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


class TestAbsenceIsNotAnError:
    def test_no_bucket_configured_returns_nothing(self):
        """The normal state of this repository: no vendor chosen, no bucket, no credential.
        The integration test skips, and that must not look like a misconfiguration."""
        assert check({}, app_bucket=None) is None

    def test_an_empty_bucket_name_is_the_same_as_none(self):
        assert check({"TEST_S3_BUCKET": "   "}, app_bucket=None) is None


class TestTheOptInIsNotOptional:
    def test_a_bucket_without_the_opt_in_is_refused(self):
        """A bucket name can be inherited from a shell or a CI template. The opt-in variable
        has no meaning outside this suite, so only a person can have set it."""
        with pytest.raises(UnsafeTestBucket) as caught:
            check(_env(TEST_S3_DESTRUCTIVE=None), app_bucket=None)

        assert "TEST_S3_DESTRUCTIVE" in str(caught.value)

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
    def test_only_an_affirmative_counts(self, value):
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_DESTRUCTIVE=value), app_bucket=None)

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_usual_affirmatives_are_accepted(self, value):
        assert check(_env(TEST_S3_DESTRUCTIVE=value), app_bucket=None) is not None


class TestTheBucketMustSayWhatItIs:
    def test_a_name_without_the_marker_is_refused(self):
        with pytest.raises(UnsafeTestBucket) as caught:
            check(_env(TEST_S3_BUCKET="talviora-resumes"), app_bucket=None)

        assert "test" in str(caught.value)

    @pytest.mark.parametrize(
        "name",
        [
            "talviora-test-prod",
            "test-live-resumes",
            "talviora-test-backup",
            "test-archive-2026",
        ],
    )
    def test_a_name_that_also_says_production_is_refused(self, name):
        """Containing "test" is not enough if it also contains "prod". A bucket named like
        that is not a disposable one, whatever else it is called."""
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_BUCKET=name), app_bucket=None)

    def test_the_applications_own_bucket_is_refused(self):
        """The most dangerous single mistake, checked as a fact rather than left to the naming
        convention -- which is only a convention."""
        with pytest.raises(UnsafeTestBucket) as caught:
            check(_env(), app_bucket="talviora-erasure-test")

        assert "STORAGE_S3_BUCKET" in str(caught.value)

    def test_the_comparison_ignores_case_and_spacing(self):
        with pytest.raises(UnsafeTestBucket):
            check(_env(), app_bucket="  Talviora-Erasure-Test ")


class TestThePrefixConfinesTheTest:
    def test_it_is_required(self):
        """Without one the test is confined to the bucket itself, so a mistake reaches
        everything in it."""
        with pytest.raises(UnsafeTestBucket) as caught:
            check(_env(TEST_S3_PREFIX=None), app_bucket=None)

        assert "TEST_S3_PREFIX" in str(caught.value)

    def test_it_must_end_with_a_separator(self):
        """`ci-erasure` also matches `ci-erasure-production/`, which is how a confined test
        stops being confined."""
        with pytest.raises(UnsafeTestBucket) as caught:
            check(_env(TEST_S3_PREFIX="ci-erasure"), app_bucket=None)

        assert "/" in str(caught.value)

    @pytest.mark.parametrize("prefix", ["*", "ci-*/", "ci-?/", "ci-[ab]/", "*/"])
    def test_a_wildcard_is_refused(self, prefix):
        """S3 prefixes are literal. A wildcard here is a misunderstanding, and acting on one
        would mean deleting under a prefix nobody chose."""
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_PREFIX=prefix), app_bucket=None)

    def test_a_leading_slash_is_refused(self):
        """S3 keys have no leading slash, so such a prefix matches nothing -- and a test
        confined to nothing would pass without having checked anything."""
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_PREFIX="/ci-erasure/"), app_bucket=None)

    def test_traversal_is_refused(self):
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_PREFIX="ci/../"), app_bucket=None)

    @pytest.mark.parametrize("prefix", ["a/", "x/", "ab/"])
    def test_a_prefix_too_short_to_be_deliberate_is_refused(self, prefix):
        with pytest.raises(UnsafeTestBucket):
            check(_env(TEST_S3_PREFIX=prefix), app_bucket=None)


class TestAValidatedTarget:
    def test_every_field_is_carried_through(self):
        target = check(
            _env(
                TEST_S3_ENDPOINT_URL="https://minio.example.com",
                TEST_S3_REGION="eu-west-1",
                TEST_S3_EXPECTED_BUCKET_OWNER="111111111111",
            ),
            app_bucket=None,
        )

        assert target == DisposableBucket(
            bucket="talviora-erasure-test",
            prefix="ci-erasure-checks/",
            endpoint="https://minio.example.com",
            region="eu-west-1",
            expected_owner="111111111111",
        )

    def test_it_cannot_widen_its_own_blast_radius(self):
        """Frozen, so a test cannot reassign the prefix it was confined to."""
        target = check(_env(), app_bucket=None)

        with pytest.raises(dataclasses.FrozenInstanceError):
            target.prefix = ""

    def test_keys_are_built_inside_the_prefix(self):
        target = check(_env(), app_bucket=None)

        assert target.key("abc.bin") == "ci-erasure-checks/abc.bin"

    @pytest.mark.parametrize("name", ["", "/absolute.bin", "../escape.bin", "a/../../b"])
    def test_a_key_that_escapes_the_prefix_is_refused(self, name):
        """Rejected rather than normalised: a key needing `..` is doing the thing the prefix
        confinement exists to prevent, and collapsing it quietly would hide that."""
        target = check(_env(), app_bucket=None)

        with pytest.raises(UnsafeTestBucket):
            target.key(name)

    def test_the_description_withholds_the_account_id(self):
        """An account id grants nothing but names an account, so it stays out of output."""
        target = check(
            _env(TEST_S3_EXPECTED_BUCKET_OWNER="999988887777"), app_bucket=None
        )

        assert "999988887777" not in target.describe()
        assert target.bucket in target.describe()

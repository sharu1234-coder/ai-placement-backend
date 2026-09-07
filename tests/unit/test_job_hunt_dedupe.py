"""Deduplication.

The asymmetry this module is built on gets the most coverage: wrongly merging two adverts
hides a real job from a student permanently and invisibly, while wrongly keeping two similar
rows is a visible cosmetic annoyance. So the tests below spend most of their attention on what
must NOT be merged.
"""

from app.services.job_hunt.dedupe import canonical_url, deduplicate
from app.services.job_hunt.providers.base import ProviderJob


def job(**overrides) -> ProviderJob:
    base = dict(
        provider="adzuna",
        provider_job_id="1",
        url="https://example.com/jobs/1",
        title="Software Engineer",
        company="Acme",
        location="Bengaluru",
    )
    base.update(overrides)
    return ProviderJob(**base)


class TestCanonicalUrl:
    def test_strips_tracking_parameters_and_fragments(self):
        assert canonical_url("https://x.com/jobs/1?utm_source=a#top") == "https://x.com/jobs/1"

    def test_lowercases_scheme_and_host_but_not_the_path(self):
        """Hosts are case-insensitive; paths are not. Lowercasing the path could merge two
        genuinely different listings on a case-sensitive server."""
        assert canonical_url("HTTPS://Example.COM/Jobs/AbC") == "https://example.com/Jobs/AbC"

    def test_trailing_slash_does_not_create_a_second_identity(self):
        assert canonical_url("https://x.com/jobs/1/") == canonical_url("https://x.com/jobs/1")

    def test_unusable_input_yields_an_empty_key_that_the_caller_ignores(self):
        assert canonical_url("") == ""
        assert canonical_url(None) == ""
        assert canonical_url("not a url") == ""


class TestDeduplicate:
    def test_same_provider_and_id_is_one_job(self):
        assert len(deduplicate([job(), job()])) == 1

    def test_same_url_across_two_providers_is_one_job(self):
        result = deduplicate(
            [job(provider="adzuna", provider_job_id="1"), job(provider="careerjet", provider_job_id="zz")]
        )
        assert len(result) == 1
        # First wins, preserving the provider's own relevance ordering.
        assert result[0].provider == "adzuna"

    def test_same_title_company_and_location_under_different_urls_is_one_job(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1"),
                job(provider_job_id="2", url="https://b.com/2"),
            ]
        )
        assert len(result) == 1

    def test_tracking_parameters_do_not_split_one_job_into_two(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1?utm_source=x", company=None),
                job(provider_job_id="2", url="https://a.com/1?utm_source=y", company=None),
            ]
        )
        assert len(result) == 1

    def test_ordering_is_preserved_for_everything_kept(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", title="First"),
                job(provider_job_id="2", url="https://a.com/2", title="Second"),
                job(provider_job_id="3", url="https://a.com/3", title="Third"),
            ]
        )
        assert [j.title for j in result] == ["First", "Second", "Third"]

    def test_empty_input_is_empty_output(self):
        assert deduplicate([]) == []


class TestDoesNotOverMerge:
    """Every test here asserts two adverts stay separate. A regression in any of them silently
    removes a real job opportunity from a student's results."""

    def test_different_seniority_of_the_same_role_stays_separate(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", title="Software Engineer"),
                job(provider_job_id="2", url="https://a.com/2", title="Software Engineer II"),
            ]
        )
        assert len(result) == 2

    def test_the_same_role_at_two_companies_stays_separate(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", company="Acme"),
                job(provider_job_id="2", url="https://a.com/2", company="Globex"),
            ]
        )
        assert len(result) == 2

    def test_the_same_role_in_two_cities_stays_separate(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", location="Bengaluru"),
                job(provider_job_id="2", url="https://a.com/2", location="Hyderabad"),
            ]
        )
        assert len(result) == 2

    def test_two_adverts_both_missing_a_company_are_not_thereby_the_same_job(self):
        """A shared blank is not evidence of identity. Treating it as a match is exactly the
        over-merge this module refuses to make -- which is why _content_key returns None when
        any of title/company/location is absent."""
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", company=None),
                job(provider_job_id="2", url="https://b.com/2", company=None),
            ]
        )
        assert len(result) == 2

    def test_two_adverts_both_missing_a_location_are_not_merged(self):
        result = deduplicate(
            [
                job(provider_job_id="1", url="https://a.com/1", location=None),
                job(provider_job_id="2", url="https://b.com/2", location=None),
            ]
        )
        assert len(result) == 2

    def test_the_same_id_from_two_different_providers_stays_separate(self):
        """Provider ids are opaque and only unique within a provider -- both aggregators
        numbering their first result "1" is entirely plausible."""
        result = deduplicate(
            [
                job(provider="adzuna", provider_job_id="1", url="https://a.com/1", title="A", company="X", location="Y"),
                job(provider="careerjet", provider_job_id="1", url="https://b.com/1", title="B", company="X", location="Y"),
            ]
        )
        assert len(result) == 2

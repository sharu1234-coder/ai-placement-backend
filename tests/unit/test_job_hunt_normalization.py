"""Role and skill normalization, and the URL allowlist.

The tests that matter most here are the negative ones. Normalization is where a matching
engine quietly starts lying: an over-eager synonym credits a student with a skill they never
claimed, an over-eager role alias tells them a job is their target role when it is not, and an
unvalidated URL hands a hostile link to a browser intent on the device.
"""

import pytest

from app.services.job_hunt.normalization import (
    extract_job_skills,
    normalize_skills,
    role_family,
    safe_external_url,
    seniority_from_title,
    skill_key,
    skills_intersection,
    work_mode_from_text,
)


class TestSkillKey:
    def test_synonyms_resolve_to_one_canonical_key(self):
        assert skill_key("JS") == "javascript"
        assert skill_key("NodeJS") == "node.js"
        assert skill_key("K8s") == "kubernetes"
        assert skill_key("Postgres") == "postgresql"

    def test_java_does_not_become_javascript(self):
        """The classic normalization bug. If "java" mapped to "javascript" (or matched it as a
        prefix), every Java developer would be credited with JavaScript and shown front-end
        jobs as strong matches."""
        assert skill_key("Java") == "java"
        assert skill_key("Java") != skill_key("JavaScript")

    def test_plus_and_hash_survive_because_they_distinguish_real_languages(self):
        """Stripping punctuation would collapse c, c++ and c# into one key and credit a C
        programmer with C++."""
        assert skill_key("C++") == "c++"
        assert skill_key("C#") == "c#"
        assert skill_key("C") == "c"
        assert len({skill_key("C"), skill_key("C++"), skill_key("C#")}) == 3

    def test_an_unmapped_skill_falls_through_to_itself_rather_than_being_dropped(self):
        assert skill_key("Rust") == "rust"

    def test_blank_input_yields_an_empty_key_not_a_match_everything_key(self):
        assert skill_key("") == ""
        assert skill_key("   ") == ""


class TestNormalizeSkills:
    def test_deduplicates_across_spellings_while_preserving_first_seen_order(self):
        assert normalize_skills(["React", "JS", "reactjs", "JavaScript"]) == ["react", "javascript"]

    def test_blank_and_non_string_entries_are_skipped(self):
        assert normalize_skills(["Python", "", None, 42, "  "]) == ["python"]

    def test_empty_input_is_an_empty_list(self):
        assert normalize_skills([]) == []


class TestRoleFamily:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Software Engineer", "software_engineer"),
            ("SDE 1", "software_engineer"),
            ("Senior Backend Developer", "software_engineer"),
            ("Data Analyst", "data_analyst"),
            ("Machine Learning Engineer", "data_scientist"),
            ("Android Developer", "mobile_engineer"),
            ("Associate Product Manager", "product_manager"),
        ],
    )
    def test_maps_real_advert_titles_onto_families(self, title, expected):
        assert role_family(title) == expected

    def test_sde_does_not_match_inside_sdet(self):
        """Word-boundary matching, not substring. An SDET is a QA role; matching "sde" inside
        it would file every test-engineering job under software engineering."""
        assert role_family("SDET") == "qa_engineer"

    def test_an_unrecognised_title_returns_none_rather_than_a_guess(self):
        """None is a real answer the engine reports as an unknown factor. Guessing a family
        would silently mis-rank every result for that student."""
        assert role_family("Chief Happiness Officer") is None
        assert role_family("") is None
        assert role_family(None) is None


class TestSeniorityFromTitle:
    def test_intern_wins_over_senior_because_it_is_checked_first(self):
        """"Senior Software Engineer Intern" is an internship. Ordering most-specific-first is
        what makes this come out right."""
        assert seniority_from_title("Senior Software Engineer Intern") == "intern"

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Sr. Data Engineer", "senior"),
            ("Principal Architect", "senior"),
            ("Graduate Trainee", "fresher"),
            ("Entry-Level Developer", "fresher"),
            ("Engineering Manager", "lead"),
        ],
    )
    def test_reads_common_seniority_words(self, title, expected):
        assert seniority_from_title(title) == expected

    def test_a_title_that_says_nothing_returns_none(self):
        assert seniority_from_title("Software Engineer") is None


class TestWorkModeFromText:
    def test_hybrid_is_checked_before_remote(self):
        """"Hybrid remote" is hybrid. Checking remote first would mislabel it."""
        assert work_mode_from_text("Engineer", "This is a hybrid remote role") == "hybrid"

    def test_detects_remote_and_onsite(self):
        assert work_mode_from_text("Remote Developer", None) == "remote"
        assert work_mode_from_text("Developer", "Work from office, 5 days") == "onsite"

    def test_silence_yields_none_so_the_ui_omits_work_mode_entirely(self):
        """Not "onsite". Assuming a default here would state something the advert never said."""
        assert work_mode_from_text("Software Engineer", "Join our team") is None
        assert work_mode_from_text(None, None) is None


class TestExtractJobSkills:
    def test_finds_single_and_multi_word_skills(self):
        found = extract_job_skills(
            "Backend Engineer", "You will use Python, PostgreSQL and machine learning daily."
        )
        assert "python" in found
        assert "postgresql" in found
        assert "machine learning" in found

    def test_does_not_invent_skills_from_company_or_place_names(self):
        """An open-ended extractor would pull "Bengaluru" and "Acme" into required skills, and
        each would then be shown to the student as a missing skill to go and learn."""
        found = extract_job_skills("Engineer at Acme Bengaluru", "Acme is hiring in Bengaluru.")
        assert "acme" not in found
        assert "bengaluru" not in found

    def test_no_recognisable_skills_is_an_empty_list(self):
        assert extract_job_skills("Manager", "Great culture, free snacks.") == []
        assert extract_job_skills(None, None) == []


class TestSkillsIntersection:
    def test_splits_into_matched_and_missing(self):
        matched, missing = skills_intersection(["python", "sql"], ["python", "kubernetes"])
        assert matched == ["python"]
        assert missing == ["kubernetes"]

    def test_a_student_with_no_skills_matches_nothing_rather_than_everything(self):
        matched, missing = skills_intersection([], ["python"])
        assert matched == []
        assert missing == ["python"]


class TestSafeExternalUrl:
    def test_allows_ordinary_public_https_links(self):
        assert safe_external_url("https://www.adzuna.co.in/jobs/details/123") is not None

    @pytest.mark.parametrize(
        "hostile",
        [
            "javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "intent://scan/#Intent;scheme=zxing;end",
            "file:///etc/passwd",
        ],
    )
    def test_rejects_non_http_schemes_that_the_device_would_otherwise_act_on(self, hostile):
        """The Android client hands this URL to a browser intent, so a non-http scheme is a
        live capability, not a cosmetic problem."""
        assert safe_external_url(hostile) is None

    @pytest.mark.parametrize(
        "internal",
        [
            "http://localhost:8000/admin",
            "http://127.0.0.1/",
            "http://127.1.2.3/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.9/",
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/computeMetadata/v1/",
        ],
    )
    def test_rejects_addresses_that_point_back_into_our_own_infrastructure(self, internal):
        """Applied at storage time so a future server-side fetch of a listing URL cannot become
        an SSRF by inheriting unvalidated rows written today. 169.254.169.254 is the cloud
        metadata endpoint -- the highest-value SSRF target there is."""
        assert safe_external_url(internal) is None

    @pytest.mark.parametrize(
        "obfuscated",
        [
            "http://2130706433/admin",          # 127.0.0.1 as one decimal integer
            "http://0x7f000001/admin",          # the same address in hex
            "http://0177.0.0.1/admin",          # octal first octet
            "http://[::1]/admin",               # IPv6 loopback
            "http://[::ffff:127.0.0.1]/admin",  # IPv4-mapped IPv6 loopback
            "http://[fd00::1]/admin",           # IPv6 unique-local, the v6 RFC1918
            "http://[fe80::1]/admin",           # IPv6 link-local
        ],
    )
    def test_rejects_the_same_internal_addresses_written_a_different_way(self, obfuscated):
        """Every one of these reached the same places the dotted-quad forms above do, and every
        one of them was accepted before.

        The first check matched on string prefixes ("127.", "10.", "192.168."), which is a test
        of how an address is *spelled* rather than where it points. All seven spellings here
        resolve to loopback or to a private range, and a prefix check cannot see any of them --
        including the entire IPv6 family, of which only the literal "::1" was listed by name.
        """
        assert safe_external_url(obfuscated) is None

    def test_a_public_address_in_the_same_shape_as_a_private_one_is_still_allowed(self):
        """172.32.x is outside the RFC1918 172.16-172.31 range, so the regex must not be a
        loose "172." prefix check."""
        assert safe_external_url("http://172.32.0.1/jobs") is not None

    @pytest.mark.parametrize(
        "special",
        [
            "http://224.0.0.1/admin",          # IPv4 multicast, "all hosts on this subnet"
            "http://239.255.255.250/admin",    # SSDP -- every UPnP device on the LAN
            "http://[ff02::1]/admin",          # IPv6 all-nodes multicast
            "http://255.255.255.255/admin",    # IPv4 broadcast
            "http://[::]/admin",               # unspecified
            "http://100.64.0.1/admin",         # CGNAT shared address space
        ],
    )
    def test_rejects_multicast_broadcast_and_other_non_unicast_addresses(self, special):
        """`ipaddress.is_global` is not sufficient on its own: it reports 224.0.0.1 as global,
        and an address meaning "every device on this network" is plainly not where a job advert
        lives. Multicast is therefore checked separately from the global test."""
        assert safe_external_url(special) is None

    def test_a_public_ip_literal_is_still_allowed(self):
        """Rejecting every numeric host would be the easy over-correction. Some job boards do
        serve adverts from a bare public address, and that is not our business to block."""
        assert safe_external_url("https://8.8.8.8/jobs/1") is not None

    @pytest.mark.parametrize(
        "real",
        [
            "https://www.adzuna.co.uk/land/ad/1234567890",
            "https://www.careerjet.co.in/jobad/inabcdef",
            "http://jobs.example.co.in/vacancy?id=7#apply",
            "https://sub.domain.example.museum/a",
        ],
    )
    def test_the_shapes_real_job_adverts_actually_take_are_untouched(self, real):
        """The guard against over-blocking. The new check requires a dotted host whose last
        label contains a letter, and this is the evidence that no real advert URL fails it."""
        assert safe_external_url(real) == real

    def test_a_hostname_is_accepted_on_its_name_and_that_limit_is_deliberate(self):
        """Pins the exact contract so nobody mistakes this for a complete SSRF defence.

        DNS is not resolved. "internal.corp.example" may point at 10.0.0.5 and is accepted.
        Resolving here would prove nothing about where a later request goes -- the name can
        resolve differently a second later, which is DNS rebinding, and a storage-time check
        is the shape it defeats. The guarantee is exact and narrower than it looks: an address
        *literal* cannot name a non-public host. A server-side fetch, if one is ever added,
        must check at connect time against the address actually connected to.
        """
        assert safe_external_url("http://internal.corp.example/jobs") is not None

    def test_none_and_empty_are_rejected_without_raising(self):
        assert safe_external_url(None) is None
        assert safe_external_url("") is None

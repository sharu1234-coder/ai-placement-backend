"""The Alembic configuration boundary: a percent-encoded URL must survive it.

Stage F failed 46 tests and errored 14 more at a single line of `alembic/env.py`:
`config.set_main_option("sqlalchemy.url", settings.database_url)`. Alembic's Config wraps
ConfigParser, which interpolates `%` in values, so `%40` -- the encoding of `@`, which any
generated password is liable to contain -- is invalid interpolation syntax and raises.

The raised message quotes the value it rejected, which is how a test-role password reached a
saved traceback. Both halves are tested here: that the URL now loads and round-trips, and
that nothing on the failure path repeats a credential.

No database is contacted. Every URL below is synthetic.
"""

import pytest
from alembic.config import Config

from app.database.alembic_url import escape_for_config, redact, set_sqlalchemy_url

# Synthetic. The percent-encoded parts spell "p@ss:w/rd" -- every character that has to
# survive both URL encoding and ConfigParser interpolation.
_PASSWORD = "p%40ss%3Aw%2Frd"
_URL = f"postgresql+psycopg2://talviora_test:{_PASSWORD}@127.0.0.1:55432/talviora_test"


@pytest.fixture()
def config() -> Config:
    return Config()


class TestThePercentEncodedUrlLoads:
    def test_setting_it_does_not_raise(self, config):
        set_sqlalchemy_url(config, _URL)

    def test_the_naive_call_is_what_broke(self, config):
        """The regression is real, not hypothetical.

        If a future Alembic escapes on its own behalf this test fails, which is the signal
        to simplify `set_sqlalchemy_url` -- not to delete the escaping blindly.
        """
        with pytest.raises(ValueError):
            config.set_main_option("sqlalchemy.url", _URL)

    def test_a_url_without_a_percent_also_works(self, config):
        plain = "postgresql+psycopg2://u:p@127.0.0.1:55432/talviora_test"
        set_sqlalchemy_url(config, plain)
        assert config.get_main_option("sqlalchemy.url") == plain

    def test_a_percent_in_the_database_name_survives(self, config):
        odd = "postgresql+psycopg2://u:p@127.0.0.1:55432/odd%5Fname_test"
        set_sqlalchemy_url(config, odd)
        assert config.get_main_option("sqlalchemy.url") == odd

    def test_an_empty_url_is_refused(self, config):
        with pytest.raises(ValueError):
            set_sqlalchemy_url(config, "")


class TestReadingItBackPreservesTheUrl:
    def test_the_round_trip_is_exact(self, config):
        set_sqlalchemy_url(config, _URL)
        assert config.get_main_option("sqlalchemy.url") == _URL

    def test_the_encoding_is_not_stripped(self, config):
        """The fix must not "solve" the problem by decoding the credential."""
        set_sqlalchemy_url(config, _URL)
        read_back = config.get_main_option("sqlalchemy.url")
        assert _PASSWORD in read_back
        assert "p@ss:w/rd" not in read_back

    def test_what_is_stored_is_escaped_and_doubling_is_the_only_change(self):
        assert escape_for_config(_URL) == _URL.replace("%", "%%")
        assert escape_for_config("no percent here") == "no percent here"

    def test_sqlalchemy_can_still_parse_what_came_back(self, config):
        """The point of the round trip: Alembic hands this to create_engine."""
        from sqlalchemy.engine import make_url

        set_sqlalchemy_url(config, _URL)
        parsed = make_url(config.get_main_option("sqlalchemy.url"))
        assert parsed.host == "127.0.0.1"
        assert parsed.port == 55432
        assert parsed.database == "talviora_test"
        assert parsed.password == "p@ss:w/rd"


class TestCredentialsNeverReachAMessage:
    def test_a_mismatched_round_trip_reports_without_the_password(self, config, monkeypatch):
        monkeypatch.setattr(config, "get_main_option", lambda *_a, **_k: "something else")
        with pytest.raises(ValueError) as exc:
            set_sqlalchemy_url(config, _URL)
        message = str(exc.value)
        assert _PASSWORD not in message
        assert "p@ss:w/rd" not in message
        assert "<redacted>" in message

    def test_a_rejected_url_reports_without_the_password(self, config, monkeypatch):
        def refuse(*_a, **_k):
            raise ValueError(f"invalid interpolation syntax in {_URL!r} at position 1")

        monkeypatch.setattr(config, "set_main_option", refuse)
        with pytest.raises(ValueError) as exc:
            set_sqlalchemy_url(config, _URL)
        assert _PASSWORD not in str(exc.value)
        assert "<redacted>" in str(exc.value)

    def test_the_underlying_error_really_does_leak(self):
        """Why the wrapper exists, asserted rather than asserted-about.

        ConfigParser's own exception quotes the value. Nothing here is a credential -- the
        password is synthetic -- but this is the mechanism that leaked a real one.
        """
        config = Config()
        with pytest.raises(ValueError) as exc:
            config.set_main_option("sqlalchemy.url", _URL)
        assert _PASSWORD in str(exc.value)

    def test_redact_removes_the_credential(self):
        assert redact(_URL) == "postgresql+psycopg2://<redacted>@127.0.0.1:55432/talviora_test"
        assert _PASSWORD not in redact(_URL)

    def test_redact_handles_a_url_with_no_credential(self):
        assert redact("postgresql://127.0.0.1:55432/db") == "postgresql://127.0.0.1:55432/db"

    def test_redact_handles_nonsense(self):
        assert redact("") == "<empty>"
        assert redact("not-a-url") == "<unparseable url>"

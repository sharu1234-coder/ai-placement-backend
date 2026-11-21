"""What gets handed to pg_dump and pg_restore.

Stage F's backup test asked for `127.0.0.1:55432` and the output showed pg_dump trying
`localhost:5432` with no password. The cause was not the cluster and not the credential: a
SQLAlchemy URL was passed where libpq expects a connection string. libpq recognises a URI
only when it starts with `postgresql://` or `postgres://`, so `postgresql+psycopg2://...`
was taken for a literal database *name*, and every connection parameter fell back to a
default -- including port 5432.

These tests never run the real tools except to read `--version`. `subprocess.run` is
captured, so nothing connects anywhere.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.database.pg_client import (
    ConnectionTargetError,
    Target,
    child_env,
    parse_target,
    redact_output,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import backup_db, restore_db  # noqa: E402

# Synthetic throughout: "p@ss:w/rd" percent-encoded, as a generated password would arrive.
_PASSWORD_PLAIN = "p@ss:w/rd"
_PASSWORD_ENCODED = "p%40ss%3Aw%2Frd"
_URL = f"postgresql+psycopg2://talviora_test:{_PASSWORD_ENCODED}@127.0.0.1:55432/talviora_test"

# The newest PostgreSQL server this project is actually tested against: CI's `test-postgres` job
# runs `image: postgres:17`, and docs/PRODUCTION_DEPLOYMENT.md records "Verified against 17.11".
# That document accepts servers 15+, but the *client* floor is set by the newest server we test,
# because pg_dump refuses to dump a server newer than its own major version.
_SERVER_MAJOR_BASELINE = 17

_VERSION_LINE = re.compile(r"\(PostgreSQL\)\s+(\d+)(?:\.(\d+))*")


def _parse_client_major(version_output: str) -> int:
    """The major version from a `<tool> --version` line.

    Parsed, not string-compared: distribution packaging appends its own build description, so the
    same client prints "17.11" from the EDB installer and
    "18.7 (Ubuntu 18.7-1.pgdg26.04+2)" from the PGDG apt repository.

    Raises ValueError when no version can be read. An unreadable line must not be treated as
    satisfied -- that is how "there is no client here at all" quietly passes a version check.

    For releases before PostgreSQL 10 the major version was two components (9.6 and 9.5 are
    different majors). This returns the first component, 9, for all of them. The distinction does
    not matter here because every 9.x is far below the baseline and is rejected either way.
    """
    match = _VERSION_LINE.search(version_output)
    if not match:
        raise ValueError(
            f"could not read a PostgreSQL version from {version_output!r}; refusing to treat an "
            "unparseable --version line as a compatible client"
        )
    return int(match.group(1))


class _Captured:
    """Stands in for subprocess.run and records exactly what would have been executed."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        self.argv = list(argv)
        self.env = dict(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)


@pytest.fixture()
def dump(monkeypatch, tmp_path):
    """backup_postgres with subprocess captured and a resolvable pg_dump."""
    captured = _Captured()
    monkeypatch.setattr(backup_db.subprocess, "run", captured)
    monkeypatch.setenv("PG_DUMP_PATH", r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe")
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))

    def run(url: str = _URL):
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        # The capture reports success without writing a file, so the artifact assertion in
        # the caller is not what is under test here; the arguments are.
        (out / "placeholder").write_text("")
        backup_db.backup_postgres(url, out)
        return captured

    return run


@pytest.fixture()
def restore(monkeypatch, tmp_path):
    captured = _Captured()
    monkeypatch.setattr(restore_db.subprocess, "run", captured)
    monkeypatch.setenv("PG_RESTORE_PATH", r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe")
    monkeypatch.setenv("PGPASSFILE", str(tmp_path / "pgpass"))
    artifact = tmp_path / "dump.dump"
    artifact.write_bytes(b"x")

    def run(url: str = _URL):
        restore_db.restore_postgres(url, artifact)
        return captured

    return run


def _value_after(argv: list[str], flag: str) -> str:
    assert flag in argv, f"{flag} was not passed at all: {argv}"
    return argv[argv.index(flag) + 1]


class TestHostAndPortArePassedExplicitly:
    def test_pg_dump_gets_all_four_parameters(self, dump):
        argv = dump().argv
        assert _value_after(argv, "-h") == "127.0.0.1"
        assert _value_after(argv, "-p") == "55432"
        assert _value_after(argv, "-U") == "talviora_test"
        assert _value_after(argv, "-d") == "talviora_test"

    def test_pg_restore_gets_all_four_parameters(self, restore):
        argv = restore().argv
        assert _value_after(argv, "-h") == "127.0.0.1"
        assert _value_after(argv, "-p") == "55432"
        assert _value_after(argv, "-U") == "talviora_test"
        assert _value_after(argv, "-d") == "talviora_test"

    def test_the_port_comes_from_the_url_and_is_not_a_default(self, dump):
        argv = dump("postgresql+psycopg2://u:p@127.0.0.1:55999/talviora_test").argv
        assert _value_after(argv, "-p") == "55999"

    def test_restore_keeps_its_clean_flags(self, restore):
        argv = restore().argv
        for flag in ("--clean", "--if-exists", "--no-owner"):
            assert flag in argv


class TestADialectUrlIsNeverPassedAsAConnectionString:
    def test_no_argument_is_a_url(self, dump):
        for arg in dump().argv:
            assert "://" not in arg

    def test_no_argument_names_the_dialect(self, dump):
        assert not any("psycopg2" in arg for arg in dump().argv)

    def test_the_same_holds_for_restore(self, restore):
        for arg in restore().argv:
            assert "://" not in arg
            assert "psycopg2" not in arg

    def test_a_plain_libpq_url_is_accepted_too(self):
        target = parse_target("postgresql://u:p@127.0.0.1:55432/talviora_test")
        assert target.host == "127.0.0.1"
        assert target.port == 55432


class TestPasswordsAppearInNoArgumentAndNoLog:
    def test_not_in_the_dump_arguments(self, dump):
        for arg in dump().argv:
            assert _PASSWORD_PLAIN not in arg
            assert _PASSWORD_ENCODED not in arg

    def test_not_in_the_restore_arguments(self, restore):
        for arg in restore().argv:
            assert _PASSWORD_PLAIN not in arg
            assert _PASSWORD_ENCODED not in arg

    def test_not_in_a_failure_message(self, monkeypatch, tmp_path):
        """libpq echoes what it could not use. The message must be scrubbed anyway."""
        leaky = _Captured(returncode=1, stderr=f'connection to "{_PASSWORD_PLAIN}" failed')
        monkeypatch.setattr(backup_db.subprocess, "run", leaky)
        monkeypatch.setenv("PG_DUMP_PATH", "pg_dump")
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(SystemExit) as exc:
            backup_db.backup_postgres(_URL, out)
        assert _PASSWORD_PLAIN not in str(exc.value)
        assert "<redacted>" in str(exc.value)
        assert "127.0.0.1:55432/talviora_test" in str(exc.value)

    def test_a_restore_warning_is_scrubbed(self, monkeypatch, tmp_path, capsys):
        noisy = _Captured(returncode=0, stderr=f"warning near {_PASSWORD_PLAIN}")
        monkeypatch.setattr(restore_db.subprocess, "run", noisy)
        monkeypatch.setenv("PG_RESTORE_PATH", "pg_restore")
        artifact = tmp_path / "d.dump"
        artifact.write_bytes(b"x")
        restore_db.restore_postgres(_URL, artifact)
        assert _PASSWORD_PLAIN not in capsys.readouterr().err

    def test_redact_output_handles_the_empty_cases(self):
        assert redact_output(None, "p") == ""
        assert redact_output("clean", None) == "clean"

    def test_an_existing_pgpassfile_is_not_overridden(self, dump):
        """libpq prefers PGPASSWORD, so the harness's restricted file must win."""
        env = dump().env
        assert env.get("PGPASSFILE")
        assert "PGPASSWORD" not in env

    def test_without_a_password_file_the_url_password_goes_via_the_environment(self):
        target = parse_target(_URL)
        env = child_env(target, {"PATH": "x"})
        assert env["PGPASSWORD"] == _PASSWORD_PLAIN  # decoded, as libpq expects
        assert "PGPASSWORD" not in target.args()


class TestFailureCannotFallBackToAnotherPort:
    def test_a_url_without_a_port_is_refused(self):
        with pytest.raises(ConnectionTargetError) as exc:
            parse_target("postgresql+psycopg2://u:p@127.0.0.1/talviora_test")
        assert "port" in str(exc.value)

    def test_the_refusal_names_5432_as_the_reason(self):
        """The default this guards against is the thing to say out loud."""
        with pytest.raises(ConnectionTargetError) as exc:
            parse_target("postgresql+psycopg2://u:p@127.0.0.1/talviora_test")
        assert "5432" in str(exc.value)

    def test_a_url_without_a_host_is_refused(self):
        with pytest.raises(ConnectionTargetError):
            parse_target("postgresql+psycopg2:///talviora_test")

    def test_a_url_without_a_database_is_refused(self):
        with pytest.raises(ConnectionTargetError):
            parse_target("postgresql+psycopg2://u:p@127.0.0.1:55432/")

    def test_a_url_without_a_user_is_refused(self):
        with pytest.raises(ConnectionTargetError):
            parse_target("postgresql+psycopg2://127.0.0.1:55432/talviora_test")

    def test_nothing_is_executed_when_the_target_is_incomplete(self, monkeypatch, tmp_path):
        captured = _Captured()
        monkeypatch.setattr(backup_db.subprocess, "run", captured)
        monkeypatch.setenv("PG_DUMP_PATH", "pg_dump")
        out = tmp_path / "out"
        out.mkdir()
        with pytest.raises(ConnectionTargetError):
            backup_db.backup_postgres("postgresql+psycopg2://u:p@127.0.0.1/db_test", out)
        assert captured.calls == 0

    def test_an_unparseable_url_does_not_echo_itself(self):
        with pytest.raises(ConnectionTargetError) as exc:
            parse_target("postgresql+psycopg2://u:p@[::1:55432/db_test")
        assert "p@ss" not in str(exc.value)
        assert "55432" not in str(exc.value)

    def test_an_empty_url_is_refused(self):
        with pytest.raises(ConnectionTargetError):
            parse_target("")


class TestTheClientBinariesCanServeTheServerBaseline:
    """What the client tools must satisfy, which is compatibility -- not a particular patch.

    This previously asserted `== "pg_dump (PostgreSQL) 17.11"`, the exact string one developer
    machine prints under its pinned PATH, while skipping only when the tool was *absent*. Any
    host with a different client therefore failed rather than skipped. CI run 44942286471 hit
    exactly that: `ubuntu-latest` had rolled from Ubuntu 24.04.5 to 26.04.1, which ships client
    18.7, so both cases failed on a version that is in fact *newer* than required. The assertion
    had also never once passed in CI -- the step before it had always failed first.

    The documented rule, from PostgreSQL's pg_dump Notes: pg_dump "can also dump from PostgreSQL
    servers older than its own version", but "cannot dump from PostgreSQL servers newer than its
    own major version; it will refuse to even try". So the requirement is a *floor*, not a pin.

    pg_dump and pg_restore are checked separately and deliberately, because their contracts are
    not the same: that paragraph is on the pg_dump page, and pg_restore's documentation states no
    version-compatibility rule at all. pg_restore's floor here is its own -- it has to read an
    archive written by a client at or above the baseline, which an older pg_restore cannot.

    One caveat is recorded rather than asserted, because the documentation hedges it: a client
    *newer* than the server is fine for taking the dump, but pg_dump also warns "it is not
    guaranteed that pg_dump's output can be loaded into a server of an older major version". A
    round trip through an 18.x client back into a 17 server is therefore outside what the
    documentation guarantees, even though both halves pass the floor below. That is a property of
    the round trip, not of the client, so it belongs with the round-trip test and not here.
    """

    @pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
    def test_the_resolved_tool_can_serve_the_baseline(self, tool):
        exe = shutil.which(tool)
        if not exe:
            pytest.skip(f"{tool} is not on PATH; the version pin is asserted by Stage E 10a/10b")
        result = subprocess.run([exe, "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        major = _parse_client_major(result.stdout)
        assert major >= _SERVER_MAJOR_BASELINE, (
            f"{tool} reports major {major} ({result.stdout.strip()}), which is older than the "
            f"PostgreSQL {_SERVER_MAJOR_BASELINE} server baseline. pg_dump refuses to dump a "
            "server newer than itself, so an older client cannot back this deployment up."
        )

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # The pinned local install, and the shapes distribution packaging actually prints.
            ("pg_dump (PostgreSQL) 17.11", 17),
            ("pg_dump (PostgreSQL) 18.7 (Ubuntu 18.7-1.pgdg26.04+2)", 18),
            ("pg_restore (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)", 16),
            ("pg_restore (PostgreSQL) 17.11\n", 17),
            # Pre-10 two-part majors, where "9.6" is one major rather than 9 point 6.
            ("pg_dump (PostgreSQL) 9.6.24", 9),
        ],
    )
    def test_the_version_line_is_parsed_rather_than_string_matched(self, line, expected):
        assert _parse_client_major(line) == expected

    def test_a_postgresql_16_client_is_rejected(self):
        """The direction that actually breaks a backup: older client, newer server."""
        major = _parse_client_major("pg_dump (PostgreSQL) 16.4 (Debian 16.4-1.pgdg120+1)")
        assert major < _SERVER_MAJOR_BASELINE

    def test_a_newer_compatible_client_is_accepted(self):
        """18.7 is what CI resolves. It satisfies the floor, and failing it was the bug."""
        major = _parse_client_major("pg_dump (PostgreSQL) 18.7 (Ubuntu 18.7-1.pgdg26.04+2)")
        assert major >= _SERVER_MAJOR_BASELINE

    def test_an_unreadable_version_line_is_an_error_rather_than_a_pass(self):
        """A parser that shrugged would turn "no client at all" into a silent success."""
        for unusable in ("", "pg_dump: command not found", "pg_dump (PostgreSQL) unknown"):
            with pytest.raises(ValueError):
                _parse_client_major(unusable)

    def test_the_explicit_override_is_what_gets_executed(self, monkeypatch, tmp_path):
        captured = _Captured()
        monkeypatch.setattr(backup_db.subprocess, "run", captured)
        pinned = r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe"
        monkeypatch.setenv("PG_DUMP_PATH", pinned)
        out = tmp_path / "out"
        out.mkdir()
        backup_db.backup_postgres(_URL, out)
        assert captured.argv[0] == pinned


class TestTheTargetItself:
    def test_args_are_in_a_fixed_order_with_no_extras(self):
        target = Target("127.0.0.1", 55432, "u", "d", "secret")
        assert target.args() == ["-h", "127.0.0.1", "-p", "55432", "-U", "u", "-d", "d"]

    def test_the_redacted_form_names_no_credential(self):
        target = Target("127.0.0.1", 55432, "u", "d", "secret")
        assert target.redacted() == "127.0.0.1:55432/d"
        assert "secret" not in target.redacted()
        assert "u" not in target.redacted().replace("127.0.0.1:55432/d", "")

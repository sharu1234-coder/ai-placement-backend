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


class TestTheClientBinariesAre1711:
    """Asserted against the real executables, which only resolve under the pinned PATH.

    The isolation checks assert the same thing independently (10a and 10b), so this is a
    second, in-process witness rather than the only one. It skips where the tools are not
    resolvable, because a repository test cannot require a local PostgreSQL install -- and a
    skip here is not evidence of anything.
    """

    @pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
    def test_the_resolved_tool_reports_1711(self, tool):
        import shutil

        exe = shutil.which(tool)
        if not exe:
            pytest.skip(f"{tool} is not on PATH; the version pin is asserted by Stage E 10a/10b")
        result = subprocess.run([exe, "--version"], capture_output=True, text=True)
        assert result.returncode == 0
        assert result.stdout.strip() == f"{tool} (PostgreSQL) 17.11"

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

"""
Integration tests for the patreon-dl-docker entrypoint.

Tests are grouped by container startup state. Where multiple assertions share the
same state (TestNoDb, TestDbPresent), a single class-scoped container is started
once for the whole class rather than per-test.
"""

import shlex
import sqlite3
import time

import pytest

# A cron expression that never fires (Feb 31), so the scheduled downloader
# won't run and interfere with container state during tests.
NEVER_FIRES = "0 0 31 2 *"


def container_logs(container):
    return container.logs(stdout=True, stderr=True).decode()


def poll_until(fn, timeout=20, interval=0.5):
    """Return True as soon as fn() is truthy, or False after timeout seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Smoke — all required tools must be present and callable in pass-through mode
# ---------------------------------------------------------------------------

class TestSmoke:
    @pytest.mark.parametrize("cmd", [
        ["patreon-dl", "-h"],
        ["yt-dlp", "--version"],
        ["deno", "--version"],
        ["supercronic", "--version"],
    ])
    def test_tool_installed(self, cmd, image_tag, docker_client):
        """Each bundled tool must exit 0 when called in pass-through mode."""
        # ContainerError is raised on non-zero exit, acting as the assertion.
        docker_client.containers.run(image_tag, cmd, remove=True)

    def test_passthrough_runs_command_and_returns_output(self, image_tag, docker_client):
        """Pass-through mode must exec the given command and return its output."""
        output = docker_client.containers.run(image_tag, ["echo", "hello"], remove=True)
        assert output.strip() == b"hello"


# ---------------------------------------------------------------------------
# out.dir validation — each scenario needs only a short-lived container
# ---------------------------------------------------------------------------

class TestOutDirValidation:
    @pytest.fixture
    def make_container(self, image_tag, docker_client, tmp_path_factory):
        """Factory: start a detached container with the given config.conf content."""
        started = []

        def _make(config_content):
            dirs = tmp_path_factory.mktemp("val")
            config_dir = dirs / "config"
            config_dir.mkdir()
            downloads_dir = dirs / "downloads"
            downloads_dir.mkdir()
            if config_content is not None:
                (config_dir / "config.conf").write_text(config_content)
            c = docker_client.containers.run(
                image_tag,
                detach=True,
                environment={"CRON_SCHEDULE": NEVER_FIRES},
                volumes={
                    str(config_dir): {"bind": "/config", "mode": "ro"},
                    str(downloads_dir): {"bind": "/downloads", "mode": "rw"},
                },
            )
            started.append(c)
            return c

        yield _make

        for c in started:
            c.stop(timeout=3)
            c.remove(force=True)

    def test_rejects_custom_out_dir(self, make_container):
        """out.dir pointing outside /downloads must exit 1 with an error in logs."""
        c = make_container("out.dir = /other\n")
        result = c.wait(timeout=10)
        assert result["StatusCode"] == 1
        assert "out.dir" in container_logs(c)

    def test_accepts_downloads_out_dir(self, make_container):
        """out.dir = /downloads must not produce a validation error."""
        c = make_container("out.dir = /downloads\n")
        time.sleep(2)
        assert "ERROR: out.dir" not in container_logs(c)

    def test_accepts_missing_config(self, make_container):
        """No config.conf must skip the validation entirely without error."""
        c = make_container(None)
        time.sleep(2)
        assert "ERROR: out.dir" not in container_logs(c)

    def test_ignores_commented_out_dir(self, make_container):
        """A commented-out out.dir line must not trigger the validation error."""
        c = make_container("# out.dir = /other\n")
        time.sleep(2)
        assert "ERROR: out.dir" not in container_logs(c)


# ---------------------------------------------------------------------------
# No-DB state — one container shared across all assertions in this class
# ---------------------------------------------------------------------------

class TestNoDb:
    @pytest.fixture(scope="class")
    def container(self, image_tag, docker_client, tmp_path_factory):
        dirs = tmp_path_factory.mktemp("nodb")
        config_dir = dirs / "config"
        config_dir.mkdir()
        downloads_dir = dirs / "downloads"
        downloads_dir.mkdir()

        c = docker_client.containers.run(
            image_tag,
            detach=True,
            environment={"CRON_SCHEDULE": NEVER_FIRES},
            volumes={
                str(config_dir): {"bind": "/config", "mode": "ro"},
                str(downloads_dir): {"bind": "/downloads", "mode": "rw"},
            },
        )
        time.sleep(3)  # Let the entrypoint finish its startup path
        yield c
        c.stop(timeout=5)
        c.remove()

    def test_guidance_message_in_logs(self, container):
        """The no-DB branch must log the guidance message with the run command."""
        output = container_logs(container)
        assert "archive browser not started" in output
        assert "docker compose run" in output

    def test_container_stays_running(self, container):
        """Container must remain alive because supercronic keeps it up."""
        container.reload()
        assert container.status == "running"

    def test_server_not_listening(self, container):
        """Port 3000 must not respond when the DB is absent."""
        result = container.exec_run(
            ["curl", "-sf", "--max-time", "2", "http://127.0.0.1:3000"]
        )
        assert result.exit_code != 0

    def test_crontab_forces_no_prompt(self, container):
        """The scheduled command must force --no-prompt: cron has no TTY, so the
        confirmation prompt would crash every run before downloading."""
        result = container.exec_run(["cat", "/tmp/crontab"])
        assert result.exit_code == 0
        assert "patreon-dl --no-prompt -C /config/config.conf" in result.output.decode()

    def test_crontab_gates_on_auth_check(self, container):
        """The scheduled command must run check-auth.sh and only download when it
        returns 0 (valid) or 2 (no cookie)."""
        result = container.exec_run(["cat", "/tmp/crontab"])
        crontab = result.output.decode()
        assert "/check-auth.sh" in crontab
        assert 'rc" = 0' in crontab and 'rc" = 2' in crontab


# ---------------------------------------------------------------------------
# Preflight auth check — check-auth.sh maps the configured cookie + endpoint
# response to exit codes. The mock_auth_server / auth_network fixtures (conftest)
# keep these tests hermetic; the check never reaches Patreon.
# ---------------------------------------------------------------------------

class TestAuthCheck:
    def run_check(self, image_tag, docker_client, auth_network, config_content, status):
        """Run /check-auth.sh once against the mock returning `status`; return its exit code.

        config_content is written to /config/config.conf (None = no config file). The hold
        file lives in a fresh per-container location so runs are independent.
        """
        env = {"PATREON_AUTH_URL": f"http://mock:8080/{status}", "HOLD_FILE": "/tmp/hold"}
        if config_content is not None:
            env["_CFG"] = config_content
            cmd = ['printf "%s" "$_CFG" > /tmp/c.conf; CONFIG_FILE=/tmp/c.conf /check-auth.sh']
        else:
            cmd = ["CONFIG_FILE=/nonexistent /check-auth.sh"]
        c = docker_client.containers.run(
            image_tag, cmd,
            entrypoint=["bash", "-c"],
            environment=env,
            network=auth_network.name,
            detach=True,
        )
        try:
            return c.wait(timeout=20)["StatusCode"]
        finally:
            c.remove(force=True)

    def run_sequence(self, image_tag, docker_client, auth_network, steps):
        """Run several check-auth.sh invocations in ONE container so the on-disk hold
        persists between them. `steps` is a list of (config_content, status) tuples.
        Returns the list of exit codes, one per step."""
        # Build a script that writes each config, runs the check, and prints "rc<i>=<n>".
        lines = []
        for i, (cfg, status) in enumerate(steps):
            lines.append(f'printf "%s" {shlex.quote(cfg)} > /tmp/c.conf')
            lines.append(
                f'PATREON_AUTH_URL=http://mock:8080/{status} CONFIG_FILE=/tmp/c.conf '
                f'HOLD_FILE=/tmp/hold /check-auth.sh; echo "rc{i}=$?"'
            )
        script = "; ".join(lines)
        c = docker_client.containers.run(
            image_tag, [script],
            entrypoint=["bash", "-c"],
            network=auth_network.name,
            detach=True,
        )
        try:
            c.wait(timeout=30)
            logs = container_logs(c)
        finally:
            c.remove(force=True)
        markers = dict(
            line.split("=", 1)
            for line in logs.splitlines()
            if line.startswith("rc") and "=" in line
        )
        return [int(markers[f"rc{i}"]) for i in range(len(steps))]

    def test_valid_cookie_returns_0(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network,
                            "cookie = session_id=valid\n", 200)
        assert rc == 0

    def test_expired_cookie_returns_1(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network,
                            "cookie = session_id=expired\n", 401)
        assert rc == 1

    def test_inconclusive_returns_3(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network,
                            "cookie = session_id=whatever\n", 503)
        assert rc == 3

    def test_no_cookie_returns_2(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network, "cookie =\n", 200)
        assert rc == 2

    def test_no_config_returns_2(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network, None, 200)
        assert rc == 2

    def test_commented_cookie_returns_2(self, image_tag, docker_client, auth_network, mock_auth_server):
        rc = self.run_check(image_tag, docker_client, auth_network,
                            "# cookie = session_id=ignored\n", 200)
        assert rc == 2

    def test_held_cookie_skips_network_call(self, image_tag, docker_client, auth_network, mock_auth_server):
        """A 401 writes a hold; a second check with the same cookie returns 4 (held)
        without contacting the endpoint — even if the endpoint would now return 200."""
        codes = self.run_sequence(image_tag, docker_client, auth_network, [
            ("cookie = session_id=bad\n", 401),  # writes hold
            ("cookie = session_id=bad\n", 200),  # same cookie -> held, no network call
        ])
        assert codes == [1, 4]

    def test_cookie_change_clears_hold_and_revalidates(self, image_tag, docker_client, auth_network, mock_auth_server):
        """After a hold, a different cookie value clears the hold and re-validates."""
        codes = self.run_sequence(image_tag, docker_client, auth_network, [
            ("cookie = session_id=bad\n", 401),    # writes hold for 'bad'
            ("cookie = session_id=fixed\n", 200),  # different cookie -> clear hold, 200
        ])
        assert codes == [1, 0]

    def test_inconclusive_does_not_write_hold(self, image_tag, docker_client, auth_network, mock_auth_server):
        """An inconclusive result must not create a hold: a later valid check returns 0,
        not 4."""
        codes = self.run_sequence(image_tag, docker_client, auth_network, [
            ("cookie = session_id=x\n", 503),  # inconclusive, no hold
            ("cookie = session_id=x\n", 200),  # would be 4 if a hold had been written
        ])
        assert codes == [3, 0]


# ---------------------------------------------------------------------------
# Pass-through guard — the interactive download flow (docker compose run ...
# patreon-dl ...) must be auth-checked too. A failed check refuses before exec;
# read-only invocations and non-patreon-dl commands run unguarded.
#
# These tests assert on the entrypoint's "refusing to run" message rather than
# exit code, since an unguarded patreon-dl command still runs (and may fail for
# unrelated network reasons) — what matters is whether the guard blocked it.
# ---------------------------------------------------------------------------

REFUSE_MSG = "refusing to run"


class TestPassThroughGuard:
    def run_passthrough(self, image_tag, docker_client, auth_network, cmd, status,
                        cookie="cookie = session_id=x\n"):
        """Run the entrypoint in pass-through mode with `cmd`, against the mock
        returning `status`. Returns (exit_code, combined_logs)."""
        env = {"PATREON_AUTH_URL": f"http://mock:8080/{status}", "_CFG": cookie}
        # Override the entrypoint with a small bootstrap: write the config file, then
        # exec the real entrypoint with the pass-through args. With entrypoint
        # `bash -c <inner> _`, $0 is "_" and "$@" is the pass-through command, so the
        # real /entrypoint.sh receives exactly `cmd` as its positional args.
        inner = 'printf "%s" "$_CFG" > /config/config.conf; exec /entrypoint.sh "$@"'
        c = docker_client.containers.run(
            image_tag,
            command=cmd,
            entrypoint=["bash", "-c", inner, "_"],
            environment=env,
            network=auth_network.name,
            detach=True,
        )
        try:
            code = c.wait(timeout=30)["StatusCode"]
            return code, container_logs(c)
        finally:
            c.remove(force=True)

    def test_download_refused_on_expired_cookie(self, image_tag, docker_client, auth_network, mock_auth_server):
        """A real download invocation must be refused when the cookie is expired (401)."""
        code, logs = self.run_passthrough(
            image_tag, docker_client, auth_network,
            ["patreon-dl", "-C", "/config/config.conf", "/config/urls.txt"], 401)
        assert REFUSE_MSG in logs
        assert code == 1

    def test_download_refused_on_inconclusive(self, image_tag, docker_client, auth_network, mock_auth_server):
        """A real download invocation must be refused when auth can't be verified (5xx)."""
        code, logs = self.run_passthrough(
            image_tag, docker_client, auth_network,
            ["patreon-dl", "-C", "/config/config.conf", "/config/urls.txt"], 503)
        assert REFUSE_MSG in logs
        assert code == 1

    def test_dry_run_not_guarded(self, image_tag, docker_client, auth_network, mock_auth_server):
        """--dry-run writes nothing, so it must not be blocked even with a bad cookie."""
        _, logs = self.run_passthrough(
            image_tag, docker_client, auth_network,
            ["patreon-dl", "--dry-run", "-C", "/config/config.conf", "/config/urls.txt"], 401)
        assert REFUSE_MSG not in logs

    def test_list_tiers_not_guarded(self, image_tag, docker_client, auth_network, mock_auth_server):
        """--list-tiers is a read-only query, not a download — must not be blocked."""
        _, logs = self.run_passthrough(
            image_tag, docker_client, auth_network,
            ["patreon-dl", "--list-tiers", "someone"], 401)
        assert REFUSE_MSG not in logs

    def test_help_not_guarded(self, image_tag, docker_client, auth_network, mock_auth_server):
        """-h must not be blocked."""
        _, logs = self.run_passthrough(
            image_tag, docker_client, auth_network, ["patreon-dl", "-h"], 401)
        assert REFUSE_MSG not in logs

    def test_non_patreon_dl_command_not_guarded(self, image_tag, docker_client, auth_network, mock_auth_server):
        """A non-patreon-dl command must pass through unguarded even with a bad cookie."""
        code, logs = self.run_passthrough(
            image_tag, docker_client, auth_network, ["echo", "hi"], 401)
        assert REFUSE_MSG not in logs
        assert code == 0
        assert "hi" in logs


# ---------------------------------------------------------------------------
# Healthcheck — healthcheck.sh reports unhealthy when an auth hold is present
# or (when the server is expected) the archive server is unreachable. Tested
# directly with env overrides so no real server or network is needed.
# ---------------------------------------------------------------------------

class TestHealthcheck:
    def run_health(self, image_tag, docker_client, setup):
        """Run /healthcheck.sh after a setup script that arranges hold/marker/server
        state. Returns (exit_code, logs)."""
        script = f"{setup}; HOLD_FILE=/tmp/hold SERVER_MARKER=/tmp/marker " \
                 f"SERVER_URL=http://127.0.0.1:3000 /healthcheck.sh"
        c = docker_client.containers.run(
            image_tag, [script], entrypoint=["bash", "-c"], detach=True)
        try:
            code = c.wait(timeout=15)["StatusCode"]
            return code, container_logs(c)
        finally:
            c.remove(force=True)

    def test_healthy_no_hold_no_server_expected(self, image_tag, docker_client):
        """No hold and no server marker (scheduler-only mode) -> healthy."""
        code, _ = self.run_health(image_tag, docker_client, "true")
        assert code == 0

    def test_unhealthy_when_held(self, image_tag, docker_client):
        """A present hold file -> unhealthy, with the refresh-cookie reason logged."""
        code, logs = self.run_health(image_tag, docker_client, "echo somehash > /tmp/hold")
        assert code == 1
        assert "downloads are held" in logs

    def test_unhealthy_when_server_expected_but_down(self, image_tag, docker_client):
        """Server marker present but nothing listening on :3000 -> unhealthy."""
        code, logs = self.run_health(image_tag, docker_client, "touch /tmp/marker")
        assert code == 1
        assert "not responding" in logs

    def test_hold_takes_precedence_over_server(self, image_tag, docker_client):
        """When held, the hold reason is reported regardless of server state."""
        code, logs = self.run_health(
            image_tag, docker_client, "echo h > /tmp/hold; touch /tmp/marker")
        assert code == 1
        assert "downloads are held" in logs


# ---------------------------------------------------------------------------
# Boot with an expired cookie must NOT crash — the container stays up (server
# running), a hold is written, and the healthcheck reports unhealthy.
# ---------------------------------------------------------------------------

class TestBootWithBadCookie:
    @pytest.fixture(scope="class")
    def container(self, image_tag, docker_client, auth_network, mock_auth_server, tmp_path_factory):
        dirs = tmp_path_factory.mktemp("badcookie")
        config_dir = dirs / "config"
        config_dir.mkdir()
        (config_dir / "config.conf").write_text("cookie = session_id=expired\n")
        downloads_dir = dirs / "downloads"
        db_dir = downloads_dir / ".patreon-dl"
        db_dir.mkdir(parents=True)
        conn = sqlite3.connect(str(db_dir / "db.sqlite"))
        conn.close()

        c = docker_client.containers.run(
            image_tag,
            detach=True,
            environment={"CRON_SCHEDULE": NEVER_FIRES,
                         "PATREON_AUTH_URL": "http://mock:8080/401"},
            network=auth_network.name,
            volumes={
                str(config_dir): {"bind": "/config", "mode": "ro"},
                str(downloads_dir): {"bind": "/downloads", "mode": "rw"},
            },
        )
        time.sleep(4)  # let the boot path run the auth check and start the server
        yield c, downloads_dir
        c.stop(timeout=5)
        c.remove()

    def test_container_stays_running(self, container):
        """An expired cookie at boot must not crash the container."""
        c, _ = container
        c.reload()
        assert c.status == "running"

    def test_hold_file_written(self, container):
        """The boot auth check must write the hold file on a 401."""
        c, downloads_dir = container
        hold = downloads_dir / ".patreon-dl" / ".cookie-hold"
        assert hold.exists()

    def test_reports_unhealthy(self, container):
        """The healthcheck must report unhealthy while the cookie is held."""
        c, _ = container
        result = c.exec_run(["/healthcheck.sh"])
        assert result.exit_code == 1
        assert "held" in result.output.decode()


# ---------------------------------------------------------------------------
# DB-present state — one container shared across all assertions in this class
# ---------------------------------------------------------------------------

class TestDbPresent:
    @pytest.fixture(scope="class")
    def container(self, image_tag, docker_client, tmp_path_factory):
        dirs = tmp_path_factory.mktemp("withdb")
        config_dir = dirs / "config"
        config_dir.mkdir()
        downloads_dir = dirs / "downloads"
        db_dir = downloads_dir / ".patreon-dl"
        db_dir.mkdir(parents=True)

        # A minimal valid SQLite file is enough: the server only checks existsSync
        # before opening. openDB() creates all tables via CREATE TABLE IF NOT EXISTS.
        conn = sqlite3.connect(str(db_dir / "db.sqlite"))
        conn.close()

        c = docker_client.containers.run(
            image_tag,
            detach=True,
            environment={"CRON_SCHEDULE": NEVER_FIRES},
            volumes={
                str(config_dir): {"bind": "/config", "mode": "ro"},
                str(downloads_dir): {"bind": "/downloads", "mode": "rw"},
            },
        )

        server_up = poll_until(
            lambda: c.exec_run(
                ["curl", "-sf", "--max-time", "1", "http://127.0.0.1:3000"]
            ).exit_code == 0,
            timeout=20,
        )
        if not server_up:
            logs = container_logs(c)
            c.stop(timeout=3)
            c.remove()
            pytest.fail(f"Archive server never became available. Logs:\n{logs}")

        yield c
        c.stop(timeout=5)
        c.remove()

    def test_server_responds(self, container):
        """Archive server must respond on port 3000 when the DB is present."""
        result = container.exec_run(
            ["curl", "-sf", "--max-time", "5", "http://127.0.0.1:3000"]
        )
        assert result.exit_code == 0

    def test_container_is_running(self, container):
        """Container must remain running with both server and scheduler alive."""
        container.reload()
        assert container.status == "running"

    def test_no_db_errors_in_logs(self, container):
        """No DB-related errors must appear in the logs on a clean start."""
        output = container_logs(container)
        assert "does not exist" not in output
        assert "database is locked" not in output

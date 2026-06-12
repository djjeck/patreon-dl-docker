"""
Integration tests for the patreon-dl-docker entrypoint.

Tests are grouped by container startup state. Where multiple assertions share the
same state (TestNoDb, TestDbPresent), a single class-scoped container is started
once for the whole class rather than per-test.
"""

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

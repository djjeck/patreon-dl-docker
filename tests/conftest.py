import os
import time

import docker
import pytest


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def auth_network(docker_client):
    """A user-defined bridge network shared by the mock auth server and the
    containers under test, so the check can reach the mock by name."""
    net = docker_client.networks.create("patreon-dl-test-auth", driver="bridge")
    yield net
    net.remove()


@pytest.fixture(scope="session")
def mock_auth_server(image_tag, docker_client, auth_network):
    """A mock of Patreon's current_user endpoint, reachable on auth_network as
    http://mock:8080/. The requested path selects the status code returned:
    /200 -> 200, /401 -> 401, /503 -> 503. Keeps the auth-check tests hermetic —
    they never reach Patreon."""
    script = (
        "import http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        code = int(self.path.strip('/') or '200')\n"
        "        self.send_response(code); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "http.server.HTTPServer(('0.0.0.0', 8080), H).serve_forever()\n"
    )
    c = docker_client.containers.run(
        image_tag,
        ["python3", "-c", script],
        detach=True,
        name="mock",
        network=auth_network.name,
    )
    # Wait until the mock answers a request from a sibling container.
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        probe = docker_client.containers.run(
            image_tag,
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "1", "http://mock:8080/200"],
            remove=True, network=auth_network.name,
        )
        if probe.strip() == b"200":
            ready = True
            break
        time.sleep(0.5)
    if not ready:
        logs = c.logs().decode()
        c.stop(timeout=3)
        c.remove()
        pytest.fail(f"Mock auth server never came up. Logs:\n{logs}")
    yield c
    c.stop(timeout=3)
    c.remove()


@pytest.fixture(scope="session")
def image_tag():
    """The image under test, identified by the TEST_IMAGE_TAG environment variable.

    The suite does not build the image — building is owned by the Makefile (`make test`)
    and by CI, both of which set TEST_IMAGE_TAG to the image they built. Running bare
    pytest without it exits immediately with guidance rather than silently building for
    several minutes.
    """
    tag = os.environ.get("TEST_IMAGE_TAG")
    if not tag:
        pytest.exit(
            "TEST_IMAGE_TAG is not set. Run `make test` to build the image and run the "
            "suite, or build it yourself and set TEST_IMAGE_TAG=<tag>. See CLAUDE.md.",
            returncode=1,
        )
    return tag

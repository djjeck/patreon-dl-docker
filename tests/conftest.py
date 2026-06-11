import os
from pathlib import Path

import docker
import pytest

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_TEST_TAG = "patreon-dl-docker-test:latest"


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


@pytest.fixture(scope="session")
def image_tag(docker_client):
    """Build the image once per session, or use TEST_IMAGE_TAG if set (e.g. in CI)."""
    tag = os.environ.get("TEST_IMAGE_TAG")
    if tag:
        yield tag
        return

    print(f"\nBuilding {DEFAULT_TEST_TAG} ...")
    docker_client.images.build(path=str(PROJECT_ROOT), tag=DEFAULT_TEST_TAG, rm=True)
    yield DEFAULT_TEST_TAG
    docker_client.images.remove(DEFAULT_TEST_TAG, force=True)

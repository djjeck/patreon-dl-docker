import os

import docker
import pytest


@pytest.fixture(scope="session")
def docker_client():
    return docker.from_env()


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

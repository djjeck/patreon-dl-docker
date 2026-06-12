IMAGE := patreon-dl-docker-test:latest
VENV := .venv
PYTEST := $(VENV)/bin/pytest

.PHONY: help build test test-fast clean clean-venv

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# Create the venv and install test deps only when requirements change.
# Depending on the requirements file (not a .PHONY target) means make skips
# this when the venv is already up to date.
$(VENV): requirements-test.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-test.txt
	@touch $(VENV)

build: ## Build the test image (Docker streams the build log live)
	docker build -t $(IMAGE) .

test: $(VENV) build ## Set up the venv, build the image, then run the suite
	TEST_IMAGE_TAG=$(IMAGE) $(PYTEST) -v

test-fast: $(VENV) ## Run the suite against an already-built image (skips rebuild)
	TEST_IMAGE_TAG=$(IMAGE) $(PYTEST) -v

clean: ## Remove the test image
	-docker rmi $(IMAGE)

clean-venv: ## Remove the test virtualenv
	rm -rf $(VENV)

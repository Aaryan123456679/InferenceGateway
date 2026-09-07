.PHONY: install lint type test all migrate smoke smoke-down load load-baseline
# Order matters: this project's own deps first (pulls aikit from its pinned
# git tag), then aikit editable LAST so it overwrites that with a link to
# local source - pip always re-resolves a direct-URL dependency rather than
# treating an already-installed package as satisfying it.
install: ; pip install -e ".[dev]" && pip install -e "../aikit[gateway]"
lint:    ; ruff check .
type:    ; mypy src
test:    ; pytest -q
migrate: ; alembic upgrade head
all: lint type test
smoke:      ; ./docker/smoke.sh
smoke-down: ; docker compose -f docker/docker-compose.yml down -v

GATEWAY_URL ?= http://localhost:8000

load:            ## load test, cache ON
	GATEWAY_URL=$(GATEWAY_URL) LABEL=cache bash loadtest/run.sh

load-baseline:   ## load test, cache OFF (x-cache: no-store) - the A/B control
	GATEWAY_URL=$(GATEWAY_URL) LABEL=baseline NO_CACHE=1 bash loadtest/run.sh

# LabelLens developer entry points. Windows users: run these under Git Bash.
PY := backend/.venv/Scripts/python.exe
ifeq ($(wildcard $(PY)),)
PY := backend/.venv/bin/python
endif

.PHONY: install test lint typecheck check migrate up down cov backup restore new-rule eval \
	adversarial retention-purge \
	frontend-install frontend-test frontend-lint frontend-typecheck frontend-check frontend-e2e

install: frontend-install
	python -m venv backend/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "backend[dev]"

frontend-install:
	cd frontend && npm install

frontend-test:
	cd frontend && npm run test

frontend-lint:
	cd frontend && npm run lint

frontend-typecheck:
	cd frontend && npm run typecheck

frontend-check: frontend-lint frontend-typecheck frontend-test

frontend-e2e:
	cd frontend && npm run e2e

test:
	cd backend && ../$(PY) -m pytest -q

cov:
	cd backend && ../$(PY) -m pytest -q --cov=app --cov-report=term-missing

lint:
	cd backend && ../$(PY) -m ruff check .

typecheck:
	cd backend && ../$(PY) -m mypy

check: lint typecheck test frontend-check

# IMPLEMENTATION.md section 23's nine adversarial families (P7-T4). Also a
# required CI check - see .github/workflows/ci.yml.
adversarial:
	cd backend && ../$(PY) -m pytest -q tests/security/adversarial

migrate:
	cd backend && ../$(PY) -m alembic upgrade head

# P7-T8: nightly retention purge + two-phase deletion hard purge, against
# the real configured database and object store. See scripts/purge_retention.py.
retention-purge:
	cd backend && ../$(PY) scripts/purge_retention.py

# See docs/rules-authoring.md. e.g.:
#   make new-rule PACK=app/rulesets/in-fssai-food/v1.0.0 KEY=IN-FSSAI-FOOD-X \
#       FIELD=dates.batch_number TITLE="X is declared" CITATION="Regulation ..."
new-rule:
	cd backend && ../$(PY) scripts/new_rule.py "$(PACK)" "$(KEY)" --field "$(FIELD)" \
		--title "$(TITLE)" --citation "$(CITATION)"

up:
	docker compose -f infra/docker-compose.yml up --build

down:
	docker compose -f infra/docker-compose.yml down -v

# See docs/runbooks/backup-and-restore.md. DATABASE_URL/TARGET_DATABASE_URL
# must point at a server with a matching-major-version pg_dump/pg_restore
# on PATH - run these from inside a postgres:16-alpine container in dev.
backup:
	bash infra/scripts/backup_db.sh

restore:
	bash infra/scripts/restore_db.sh $(DUMP)

# See evals/dataset.py's own module docstring for this golden dataset's
# honestly-stated scope. Writes a versioned JSON + HTML report under
# backend/evals/reports/<timestamp>/ (gitignored - a run artifact, not
# something to commit). Pass SUBSET=N for the smaller nightly-CI run, e.g.
# `make eval SUBSET=2`.
eval:
	cd backend && ../$(PY) -m evals.cli $(if $(SUBSET),--subset $(SUBSET),)

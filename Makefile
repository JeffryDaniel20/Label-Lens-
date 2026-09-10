# LabelLens developer entry points. Windows users: run these under Git Bash.
PY := backend/.venv/Scripts/python.exe
ifeq ($(wildcard $(PY)),)
PY := backend/.venv/bin/python
endif

.PHONY: install test lint typecheck check migrate up down cov backup restore new-rule \
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

migrate:
	cd backend && ../$(PY) -m alembic upgrade head

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

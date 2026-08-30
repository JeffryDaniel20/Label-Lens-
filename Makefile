# LabelLens developer entry points. Windows users: run these under Git Bash.
PY := backend/.venv/Scripts/python.exe
ifeq ($(wildcard $(PY)),)
PY := backend/.venv/bin/python
endif

.PHONY: install test lint typecheck check migrate up down cov

install:
	python -m venv backend/.venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e "backend[dev]"

test:
	cd backend && ../$(PY) -m pytest -q

cov:
	cd backend && ../$(PY) -m pytest -q --cov=app --cov-report=term-missing

lint:
	cd backend && ../$(PY) -m ruff check .

typecheck:
	cd backend && ../$(PY) -m mypy

check: lint typecheck test

migrate:
	cd backend && ../$(PY) -m alembic upgrade head

up:
	docker compose -f infra/docker-compose.yml up --build

down:
	docker compose -f infra/docker-compose.yml down -v

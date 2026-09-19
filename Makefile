# Convenience targets. Assumes a virtualenv in .venv and PostgreSQL on localhost:5432 (docker compose up -d db).
PY      ?= .venv/bin/python
DB      ?= hamsa
DB_URL  ?= postgresql://postgres:postgres@localhost:5432/$(DB)

.PHONY: help install db real-data test rehearse run

help:
	@echo "make install    create .venv and install api/requirements.txt"
	@echo "make db         (re)build the database $(DB) from supabase/ SQL"
	@echo "make real-data  load the 529 real CDSCO NSQ rows into $(DB)"
	@echo "make test       run the automated test suite"
	@echo "make rehearse   play the demo conversation end to end (en/hi/kn), offline"
	@echo "make run        start the API on localhost:8000 (DRY_RUN=1: replies are printed, not sent)"

install:
	python3 -m venv .venv
	$(PY) -m pip install -r api/requirements.txt

db:
	$(PY) api/scripts/db_setup.py --db $(DB) --reset

real-data:
	DATABASE_URL=$(DB_URL) $(PY) api/scripts/load_real_nsq.py data/real/nsq_real_loaded.csv

test:
	DATABASE_URL=$(DB_URL) $(PY) -m pytest -q api/tests

rehearse:
	DATABASE_URL=$(DB_URL) $(PY) api/scripts/rehearse.py

run:
	cd api && DATABASE_URL=$(DB_URL) DRY_RUN=1 ../$(PY) -m uvicorn app:app --port 8000

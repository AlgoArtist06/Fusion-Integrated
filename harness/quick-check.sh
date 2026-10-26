#!/bin/sh
# Fast repository gates for the agent; prints the first failure and stops.
export DJANGO_SETTINGS_MODULE=config.settings.dev
PY=.venv/bin/python
run() { name="$1"; shift; out=$("$@" 2>&1); rc=$?; if [ $rc -ne 0 ]; then echo "FAILED: $name"; echo "$out" | tail -30; exit 1; fi; echo "ok: $name"; }
run "django check" $PY manage.py check
run "migrations in sync" $PY manage.py makemigrations --check --dry-run
run "ruff" .venv/bin/ruff check .
run "import contracts (lint-imports)" .venv/bin/lint-imports
run "no cross-module foreign keys" $PY ops/checks/no_cross_module_fk.py
run "contract getters plural" $PY ops/checks/contracts_are_plural.py
run "sidebar matches client routes" $PY ops/checks/nav_matches_routes.py
run "permission manifest current (run: make permissions)" $PY manage.py permission_manifest --check
run "openapi schema current (run: make schema)" sh -c "$PY manage.py spectacular --fail-on-warn --file /tmp/leave-schema.yaml >/dev/null && diff -q openapi/fusion-integrated.v1.yaml /tmp/leave-schema.yaml"
for d in modules/leave*/tests; do [ -d "$d" ] && run "module tests ($d)" $PY -m pytest -q -x -p no:cacheprovider "$d"; done
echo "ALL OK"

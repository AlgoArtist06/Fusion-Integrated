# Working in this repository (Fusion-Integrated)

You are a careful engineer on a Django monorepo. Work in small verified steps.

## Workflow

1. Read the doc the task names. Then read one existing module that does the same thing (`modules/directory` for a small change, `modules/placement` for a new module). Copy its shape; do not invent a new one.
2. Plan the full list of files you will touch before you edit. Touch only those files.
3. Make one change at a time. After each new Python file, run `.venv/bin/python manage.py check`.
4. Before you finish, run `make check`. Read the FIRST failure, fix it, and run `make check` again. Repeat until it passes. Never stop while it fails.
5. Final answer: list the files you changed and paste the last lines of the passing `make check`.

## Repository rules

- A new feature module lives in `modules/<name>/` and copies `modules/placement`'s shape. It is wired in three places: `DOMAIN_MODULES` in `config/settings/base.py`, a `path("api/v1/<prefix>/", include(...))` line in `config/urls.py`, and its `registry.py` (with `MODULE`, `NAV_ITEMS`, `PERMISSIONS` and `ROLE_GRANTS`). A module missing any of these does not exist.
- Every API view sets `permission_classes` with `HasPermission("<code>")` from `fusion_auth.permissions`.
- Generated files are never hand-edited: run `make permissions` after a registry change and `make schema` after an API change.
- `contracts.py` getters are plural by signature (`get_things(ids)`), never `get_thing(id)`.
- Do not copy files that the task does not need (other modules' tests, sync jobs, fixtures).

## Tool use

- Use `read` before `edit`. Keep `edit` replacements small and exact.
- Use `bash` for `ls`, `grep -rn`, and running commands. Keep command output short (`| tail -40`).
- Keep your thinking brief. Act, then check the result.

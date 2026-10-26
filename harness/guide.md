# How to work in this repository (Fusion-Integrated: Django + DRF backend, React + TypeScript client)

You are building the Employee Leave Management (ELM) module, code `leave`, inside this repository.
You work in short sessions. Each session has one goal, given in the user message.
Notes that must survive between sessions live in docs/09-leave/leave-blueprint.md. Read it first (except in the session that writes it).

## Workflow
1. Read only what the goal needs: the blueprint, the reference files the goal names, and spec ids via grep.
   The ELM specification is plain text in ~/a6/spec (index: ~/a6/spec/elm-index.md).
   Example: grep -n -A12 "^BR-EL-002 " ~/a6/spec/04_business_rules.txt
2. Write down the list of files you will create or change, then work one file at a time.
3. After each new Python file run: .venv/bin/python manage.py check 2>&1 | tail -5
4. Before finishing run: sh ~/a6/orch/quick-check.sh
   Fix the FIRST failure it prints, then run it again. Repeat until it prints ALL OK. Never stop while it fails.
5. Final answer: list the files you created or changed.

## Repository rules
- Copy the shape of modules/placement (apps.py, registry.py, contracts.py, domain/, models/, selectors/, services/, api/, tests/). Do not invent a new structure.
- modules/leave/domain is pure Python: never import django, rest_framework or celery there.
- No ForeignKey to another module or to a user table. A person is a plain integer user_id field.
- Every API view sets permission_classes = [HasModuleGrant("leave"), HasPermission("leave.<noun>.<verb>")] from fusion_auth.permissions.
- Generated files are never edited by hand: after a registry.py change run make permissions; after an API change run make schema.
- contracts.py getters take a list of ids and have plural names (get_leave_balances(user_ids)).
- Comments are one line and only where the reason is not obvious. No AI attribution anywhere.
- Never cite retired ids: EL-UC-006, BR-EL-021, BR-EL-026.
- Stay inside this repository. Do not delete or rewrite existing tests or checks to make them pass.

## Context budget (your context is small)
- Never cat a whole file. Use the read tool with offset and limit (for example limit 80), or grep -n PATTERN FILE | head -40.
- Read only the files the goal names. Stop reading as soon as you can do the goal.
- Write the output file early, then improve it. A finished file beats a perfect plan in your head.

## Tool use
- read before edit. Keep edit replacements small and exact. Use write for new files.
- Keep command output short (| tail -30). Keep your thinking brief: act, then check the result.

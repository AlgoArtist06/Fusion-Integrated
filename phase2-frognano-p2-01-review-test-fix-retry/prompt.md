Phase 2 of the ELM assignment: review, test and improve the existing module modules/leave_elm, which you wrote in Phase 1. Do not start a new module.

Do these steps in order.
1. Run sh ~/a6/orch/quick-check.sh. It fails on "sidebar matches client routes" because modules/leave_elm/registry.py has no NAV_ITEMS. This module has no client pages yet, so add NAV_ITEMS = [] next to MODULE in modules/leave_elm/registry.py. Run the check again.
2. Read modules/leave_elm/api/views.py and modules/leave_elm/api/urls.py. Then write modules/leave_elm/tests/test_api.py. For each endpoint, test three requests: one with the needed permission and the "leave_elm" module grant returns 200; one with the module grant but without the permission returns 403; one without a session returns 401 or 403. Use the fixtures in conftest.py: run grep -n "def make_session\|def make_principal\|def stub_iam" conftest.py, then read those lines with the read tool (offset and limit). Copy the style of one test in modules/placement/tests/test_api.py (grep -n "def test_" modules/placement/tests/test_api.py | head, then read one test).
3. Run .venv/bin/python -m pytest -q -p no:cacheprovider modules/leave_elm until it is green, then sh ~/a6/orch/quick-check.sh until it prints ALL OK.

Final answer: list the files you changed and every problem you found in the Phase 1 code.

"""API tests for leave_elm module."""
import pytest
from rest_framework.test import APIClient

from conftest import make_session

pytestmark = pytest.mark.django_db


def _client(stub_iam, **kw):
    stub_iam(make_session(**kw))
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION="Token abc")
    return c


def test_leave_applications_with_view_permission(stub_iam):
    """GET /api/v1/leave/applications with leave_elm.application.view returns 200."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.view",),
    ))
    c = _client(stub_iam)
    assert c.get("/api/v1/leave/applications").status_code == 200


def test_leave_applications_without_view_permission(stub_iam):
    """GET /api/v1/leave/applications with module grant but no view permission returns 403."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.apply",),
    ))
    c = _client(stub_iam)
    assert c.get("/api/v1/leave/applications").status_code == 403


def test_leave_applications_no_session(stub_iam):
    """GET /api/v1/leave/applications without session returns 401/403."""
    c = APIClient()
    # No credentials set
    assert c.get("/api/v1/leave/applications").status_code in (401, 403)


def test_leave_apply_with_apply_permission(stub_iam):
    """POST /api/v1/leave/apply with leave_elm.application.apply returns 200."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.apply",),
    ))
    c = _client(stub_iam)
    assert c.post("/api/v1/leave/apply").status_code == 200


def test_leave_apply_without_apply_permission(stub_iam):
    """POST /api/v1/leave/apply with module grant but no apply permission returns 403."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.view",),
    ))
    c = _client(stub_iam)
    assert c.post("/api/v1/leave/apply").status_code == 403


def test_leave_apply_no_session(stub_iam):
    """POST /api/v1/leave/apply without session returns 401/403."""
    c = APIClient()
    # No credentials set
    assert c.post("/api/v1/leave/apply").status_code in (401, 403)


def test_leave_approve_with_approve_permission(stub_iam):
    """POST /api/v1/leave/approve with leave_elm.application.approve returns 200."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.approve",),
    ))
    c = _client(stub_iam)
    assert c.post("/api/v1/leave/approve").status_code == 200


def test_leave_approve_without_approve_permission(stub_iam):
    """POST /api/v1/leave/approve with module grant but no approve permission returns 403."""
    stub_iam(make_session(
        user_id=1001, modules=("leave_elm",),
        permissions=("leave_elm.application.view",),
    ))
    c = _client(stub_iam)
    assert c.post("/api/v1/leave/approve").status_code == 403


def test_leave_approve_no_session(stub_iam):
    """POST /api/v1/leave/approve without session returns 401/403."""
    c = APIClient()
    # No credentials set
    assert c.post("/api/v1/leave/approve").status_code in (401, 403)

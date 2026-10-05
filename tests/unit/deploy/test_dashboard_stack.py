"""
Against a running dashboard stack (CROOM_DASHBOARD_URL plus the admin's email
and password in CROOM_DASHBOARD_EMAIL / CROOM_DASHBOARD_PASSWORD): the web app
loads from the backend, the admin signs in, a token enrolls a device, and the
register route is closed. Skipped unless the variables are set.
"""

import os
import uuid

import pytest
import requests

URL = os.environ.get("CROOM_DASHBOARD_URL")
EMAIL = os.environ.get("CROOM_DASHBOARD_EMAIL")
PASSWORD = os.environ.get("CROOM_DASHBOARD_PASSWORD")

pytestmark = pytest.mark.skipif(not (URL and EMAIL and PASSWORD), reason="no running dashboard stack configured")


def test_health_and_web_app_come_from_the_same_origin():
    assert requests.get(f"{URL}/health", timeout=5).json()["status"] == "healthy"
    page = requests.get(f"{URL}/", headers={"Accept": "text/html"}, timeout=5)
    assert page.status_code == 200 and "<title>Crystal Meet</title>" in page.text
    assert "upgrade-insecure-requests" not in page.headers.get("content-security-policy", "")
    route = requests.get(f"{URL}/settings", headers={"Accept": "text/html"}, timeout=5)
    assert route.status_code == 200 and 'id="root"' in route.text
    assert requests.get(f"{URL}/api/nope", headers={"Accept": "application/json"}, timeout=5).status_code == 404


def test_admin_signs_in_and_register_is_closed():
    login = requests.post(f"{URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=5)
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    assert requests.post(f"{URL}/api/auth/register", json={"email": "x@y.z", "password": "p", "name": "x"}, timeout=5).status_code == 401
    me = requests.get(f"{URL}/api/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=5)
    assert me.json()["role"] == "admin" and me.json()["email"] == EMAIL


def test_a_token_enrolls_a_device():
    token = requests.post(f"{URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=5).json()["token"]
    created = requests.post(f"{URL}/api/provisioning/token", json={"roomName": f"Test {uuid.uuid4().hex[:6]}"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=5)
    assert created.status_code == 201, created.text
    enrollment_token = created.json()["token"]
    enrolled = requests.post(f"{URL}/api/provisioning/enroll",
                             json={"token": enrollment_token, "deviceInfo": {"platform": "test", "softwareVersion": "0"}}, timeout=5)
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["deviceId"] == created.json()["deviceId"]
    assert enrolled.json()["websocketUrl"].endswith("/ws")

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as value:
        yield value
    close_connection()


@pytest.fixture()
def owner(client):
    user = client.post("/api/users", json={"username": "owner", "display_name": "项目负责人", "password": "OwnerPass!234"})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": "owner", "password": "OwnerPass!234"})
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


def _make_user(client, username: str, display: str) -> dict:
    password = f"Pass!{username}23456"
    user = client.post("/api/users", json={"username": username, "display_name": display, "password": password})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def paleo_project(client, owner):
    """一个植物考古项目，含 owner/researcher/recorder/reviewer/viewer 五种角色。"""
    project = client.post(
        "/api/projects",
        json={"code": "PALEO", "name": "植物考古比较研究", "site_name": "某遗址"},
        headers=owner["headers"],
    ).json()
    members = {"owner": owner}
    for username, role, display in [
        ("researcher", "researcher", "鉴定专家"),
        ("recorder", "recorder", "浮选记录员"),
        ("reviewer", "reviewer", "审核人"),
        ("viewer", "viewer", "访客"),
    ]:
        member = _make_user(client, username, display)
        response = client.post(
            f"/api/projects/{project['id']}/members",
            json={"user_id": member["user"]["id"], "role": role},
            headers=owner["headers"],
        )
        assert response.status_code == 200
        members[role] = member
    return {"project": project, "members": members}

"""鉴定变更集：待审提案、批准生成新统计版本、原始鉴定不改写、审批权限。"""
from __future__ import annotations

import pytest

from paleo_data import import_file, import_standard_dataset


def _propose(client, pid, headers, ops, note="", stats_params=None):
    body = {"note": note, "ops": ops}
    if stats_params:
        body["stats_params"] = stats_params
    response = client.post(f"/api/projects/{pid}/paleo/changesets", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_rename_approval_creates_new_version_and_keeps_raw(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    reviewer = paleo_project["members"]["reviewer"]["headers"]
    import_standard_dataset(client, pid, recorder)

    before = client.post(f"/api/projects/{pid}/paleo/stats/runs",
                         json={"rank": "species", "seed": 8, "iterations": 300}, headers=researcher)
    assert before.status_code == 201
    assert before.json()["run"]["version_no"] == 1

    changeset = _propose(
        client, pid, researcher,
        ops=[{"op": "rename", "from": "普通小麦", "to": "小麦属 spp."}],
        note="鉴定专家复核：普通小麦统一记为小麦属 spp.",
        stats_params={"rank": "species", "seed": 8, "iterations": 300},
    )
    assert changeset["status"] == "pending"

    approval = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                           json={"review_note": "同意"}, headers=reviewer)
    assert approval.status_code == 200, approval.text
    body = approval.json()
    assert body["changeset"]["status"] == "approved"
    new_run = body["stats_run"]
    assert new_run["run"]["version_no"] == 2  # 批准后生成新统计版本
    assert new_run["run"]["changeset_id"] == changeset["id"]
    assert new_run["run"]["params"]["applied_changeset_ids"] == [changeset["id"]]

    taxa = {row["taxon_key"] for row in new_run["results"]}
    assert "小麦属 spp." in taxa
    assert "普通小麦" not in taxa

    # 原始鉴定记录不被改写
    from app.database import connection
    raw = connection().execute(
        "SELECT taxon_raw, taxon_normalized, species FROM bot_identifications WHERE item_no=1"
    ).fetchone()
    assert tuple(raw) == ("小麦", "小麦", "普通小麦")


def test_merge_changeset_combines_counts(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    reviewer = paleo_project["members"]["reviewer"]["headers"]
    import_file(client, pid, recorder, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, recorder, "batches", "batch_code,sample_code,volume_liters\nB1,S1,10\n")
    import_file(client, pid, recorder, "fractions", "batch_code,fraction_type\nB1,light\n")
    import_file(client, pid, recorder, "identifications",
                "batch_code,fraction_type,item_no,taxon,count\n"
                "B1,light,1,小麦,10\nB1,light,2,普通小麦,30\n")

    changeset = _propose(
        client, pid, researcher,
        ops=[{"op": "merge", "from": ["小麦", "普通小麦"], "to": "小麦属"}],
        stats_params={"rank": "species", "seed": 4, "iterations": 300},
    )
    approval = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                           json={}, headers=reviewer)
    assert approval.status_code == 200
    run = approval.json()["stats_run"]
    merged = [r for r in run["results"] if r["taxon_key"] == "小麦属"]
    assert len(merged) == 1
    assert merged[0]["count_max"] == 40  # 两个名称合并计数
    assert merged[0]["density_point"] == pytest.approx(4.0)


def test_self_approval_forbidden(client, paleo_project):
    pid = paleo_project["project"]["id"]
    owner = paleo_project["members"]["owner"]["headers"]
    # owner 同时具备提案与审批角色，但同一人不能批准自己的提案
    changeset = _propose(client, pid, owner,
                         ops=[{"op": "rename", "from": "小麦", "to": "小麦属"}])
    response = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                           json={}, headers=owner)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "self_approval"


def test_approve_requires_reviewer_role(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    viewer = paleo_project["members"]["viewer"]["headers"]
    changeset = _propose(client, pid, researcher,
                         ops=[{"op": "rename", "from": "小麦", "to": "小麦属"}])
    assert client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                       json={}, headers=recorder).status_code == 403
    assert client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                       json={}, headers=viewer).status_code == 403
    # 提案角色也受限：记录员不能提案
    assert client.post(f"/api/projects/{pid}/paleo/changesets",
                       json={"ops": [{"op": "rename", "from": "a", "to": "b"}]},
                       headers=recorder).status_code == 403


def test_reject_changeset_creates_no_version(client, paleo_project):
    pid = paleo_project["project"]["id"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    reviewer = paleo_project["members"]["reviewer"]["headers"]
    changeset = _propose(client, pid, researcher,
                         ops=[{"op": "rename", "from": "小麦", "to": "小麦属"}])
    response = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/reject",
                           json={"review_note": "依据不足"}, headers=reviewer)
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    runs = client.get(f"/api/projects/{pid}/paleo/stats/runs", headers=reviewer).json()["data"]
    assert runs == []


def test_closed_changeset_cannot_be_reprocessed(client, paleo_project):
    pid = paleo_project["project"]["id"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    reviewer = paleo_project["members"]["reviewer"]["headers"]
    changeset = _propose(client, pid, researcher,
                         ops=[{"op": "rename", "from": "小麦", "to": "小麦属"}])
    first = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                        json={}, headers=reviewer)
    assert first.status_code == 200
    again = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/approve",
                        json={}, headers=reviewer)
    assert again.status_code == 409
    reject = client.post(f"/api/projects/{pid}/paleo/changesets/{changeset['id']}/reject",
                         json={}, headers=reviewer)
    assert reject.status_code == 409


def test_changeset_validation(client, paleo_project):
    pid = paleo_project["project"]["id"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    empty = client.post(f"/api/projects/{pid}/paleo/changesets", json={"ops": []}, headers=researcher)
    assert empty.status_code == 422
    same = client.post(f"/api/projects/{pid}/paleo/changesets",
                       json={"ops": [{"op": "rename", "from": "小麦", "to": "小麦"}]}, headers=researcher)
    assert same.status_code == 422
    merge_one = client.post(f"/api/projects/{pid}/paleo/changesets",
                            json={"ops": [{"op": "merge", "from": ["小麦"], "to": "小麦属"}]}, headers=researcher)
    assert merge_one.status_code == 422

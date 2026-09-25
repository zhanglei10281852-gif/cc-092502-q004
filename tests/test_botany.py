"""植物考古模块测试。

覆盖：零体积拒绝、部分删失（<n / nd）、重复样品与重复鉴定、固定随机种子可复算、
现代根系污染、未知分类、批次锁定、同文件重传幂等、分类口径、变更集批准与原始
鉴定不可变、项目角色权限、事务中断回滚、拒绝清单下载与离线 CLI 导入。
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys

import pytest


def make_csv(rows: list[dict[str, str]]) -> bytes:
    header = ["记录类型", "批次", "样品编号", "遗迹编号", "遗迹类型", "体积", "筛网_mm",
              "浮选组分", "分类编号", "学名", "未知", "置信度", "计数", "检测限", "污染", "备注"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in header})
    return buffer.getvalue().encode("utf-8")


def full_dataset_rows() -> list[dict[str, str]]:
    return [
        {"记录类型": "批次", "批次": "B01"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-CH-1", "遗迹编号": "C1", "遗迹类型": "古河道", "体积": "10", "筛网_mm": "0.25"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-CH-2", "遗迹编号": "C1", "遗迹类型": "古河道", "体积": "5", "筛网_mm": "0.25"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-DW-1", "遗迹编号": "F1", "遗迹类型": "居址", "体积": "8", "筛网_mm": "0.25"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-DW-2", "遗迹编号": "F1", "遗迹类型": "居址", "体积": "8", "筛网_mm": "0.25"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-PIT-1", "遗迹编号": "H1", "遗迹类型": "灰坑", "体积": "4", "筛网_mm": "0.25"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-PIT-2", "遗迹编号": "H1", "遗迹类型": "灰坑", "体积": "4", "筛网_mm": "0.25"},
        {"记录类型": "组分", "样品编号": "S-CH-1", "浮选组分": "轻"},
        {"记录类型": "组分", "样品编号": "S-CH-2", "浮选组分": "轻"},
        {"记录类型": "组分", "样品编号": "S-DW-1", "浮选组分": "轻"},
        {"记录类型": "组分", "样品编号": "S-DW-2", "浮选组分": "轻"},
        {"记录类型": "组分", "样品编号": "S-PIT-1", "浮选组分": "轻"},
        {"记录类型": "组分", "样品编号": "S-PIT-2", "浮选组分": "轻"},
        {"记录类型": "鉴定", "样品编号": "S-CH-1", "浮选组分": "轻", "分类编号": "SETARIA", "学名": "Setaria italica", "置信度": "高", "计数": "20"},
        {"记录类型": "鉴定", "样品编号": "S-CH-2", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "中", "计数": "<3", "检测限": "0.5"},
        {"记录类型": "鉴定", "样品编号": "S-DW-1", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "40"},
        {"记录类型": "鉴定", "样品编号": "S-DW-2", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "40"},
        {"记录类型": "鉴定", "样品编号": "S-PIT-1", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "nd", "检测限": "0.5"},
        {"记录类型": "鉴定", "样品编号": "S-PIT-2", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "nd", "检测限": "0.5"},
        {"记录类型": "鉴定", "样品编号": "S-PIT-1", "浮选组分": "轻", "分类编号": "UNKNOWN-GRASS", "未知": "是", "未知说明": "疑似禾本科颖果", "置信度": "存疑", "计数": "2"},
    ]


@pytest.fixture()
def project(client, owner):
    resp = client.post("/api/projects", json={"code": "BOT", "name": "植物考古比较", "site_name": "溧阳"}, headers=owner["headers"])
    assert resp.status_code == 201
    return resp.json()


@pytest.fixture()
def members(client, project):
    out = {}
    for name, role in [("scientist", "researcher"), ("rec", "recorder"), ("auditor", "reviewer"), ("guest", "viewer")]:
        client.post("/api/users", json={"username": name, "display_name": name, "password": f"{name.title()}Pass!234"})
        user = client.post("/api/sessions", json={"username": name, "password": f"{name.title()}Pass!234"}).json()
        client.post(f"/api/projects/{project['id']}/members", json={"user_id": user["user_id"], "role": role},
                    headers=_login(client, "owner", "OwnerPass!234"))
        out[role] = {"user_id": user["user_id"], "headers": {"Authorization": f"Bearer {user['token']}"}}
    return out


def _login(client, username, password):
    token = client.post("/api/sessions", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def base_url(project) -> str:
    return f"/api/projects/{project['id']}/botany"


def upload(client, project, headers, content, filename="batch.csv"):
    return client.post(f"{base_url(project)}/imports?filename={filename}", content=content,
                       headers={**headers, "Content-Type": "text/csv"})


# ---------- 导入与拒绝清单 -------------------------------------------------

def test_full_import_and_listing(client, owner, project, members):
    resp = upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["accepted_rows"] == 1 + 6 + 6 + 7  # 1 批次 + 6 样品 + 6 组分 + 7 鉴定
    assert body["rejected_rows"] == 0

    batches = client.get(f"{base_url(project)}/batches", headers=owner["headers"]).json()["data"]
    assert len(batches) == 1 and batches[0]["samples_imported"] == 6

    # viewer 只读、recorder 可写
    can_read = client.get(f"{base_url(project)}/batches", headers=members["viewer"]["headers"])
    assert can_read.status_code == 200
    no_auth = client.get(f"{base_url(project)}/batches")
    assert no_auth.status_code == 401  # Header(...) 缺失 → FastAPI 422? 实测 ServiceError 不走依赖校验
    # 无项目成员身份的用户禁止访问
    client.post("/api/users", json={"username": "outsider", "display_name": "外人", "password": "OutsiderPass!23"})
    outsider = _login(client, "outsider", "OutsiderPass!23")
    assert client.get(f"{base_url(project)}/batches", headers=outsider).status_code == 403
    # viewer 不能导入
    denied = upload(client, project, members["viewer"]["headers"], make_csv([{"记录类型": "批次", "批次": "BX"}]))
    assert denied.status_code == 403
    # recorder 可以导入
    ok = upload(client, project, members["recorder"]["headers"], make_csv([{"记录类型": "批次", "批次": "B-REC"}]), "rec.csv")
    assert ok.status_code == 201


def test_zero_volume_rejected_row_and_download(client, owner, project):
    rows = full_dataset_rows()
    rows.append({"记录类型": "土样", "批次": "B01", "样品编号": "S-ZERO", "遗迹编号": "C9", "遗迹类型": "灰坑", "体积": "0"})
    rows.append({"记录类型": "土样", "批次": "B01", "样品编号": "S-BADVOL", "遗迹编号": "C9", "遗迹类型": "灰坑", "体积": "abc"})
    body = upload(client, project, owner["headers"], make_csv(rows)).json()
    assert body["status"] == "partial"
    assert body["rejected_rows"] == 2

    # 零体积/坏数字样品没有落库
    from app.database import connection
    sample_codes = {r[0] for r in connection().execute("SELECT sample_code FROM botany_samples").fetchall()}
    assert "S-ZERO" not in sample_codes and "S-BADVOL" not in sample_codes

    resp = client.get(f"{base_url(project)}/imports/{body['import_id']}/rejections", headers=owner["headers"])
    assert resp.status_code == 200
    text = resp.content.decode("utf-8-sig")
    assert "not_positive" in text and "bad_number" in text
    assert "attachment" in resp.headers["content-disposition"]
    assert "土样体积" in text  # 中文错误信息保留


def test_duplicate_sample_and_duplicate_identification(client, owner, project):
    rows = full_dataset_rows()
    # 同文件内重复样品
    rows.append({"记录类型": "土样", "批次": "B01", "样品编号": "S-CH-1", "遗迹编号": "C1", "遗迹类型": "古河道", "体积": "9"})
    # 同文件内重复组分
    rows.append({"记录类型": "组分", "样品编号": "S-DW-1", "浮选组分": "轻"})
    # 同组分/分类/删失/计数/置信度完全重复的鉴定
    rows.append({"记录类型": "鉴定", "样品编号": "S-DW-1", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "40"})
    body = upload(client, project, owner["headers"], make_csv(rows)).json()
    assert body["rejected_rows"] == 2  # 重复样品 + 重复组分
    assert body["duplicate_lines"] == 1  # 重复鉴定跳过但不算错误

    # 跨文件重复样品：另一文件复用同批次追加同编号样品
    cross = make_csv([
        {"记录类型": "批次", "批次": "B01"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-CH-1", "遗迹编号": "C1", "遗迹类型": "古河道", "体积": "10"},
    ])
    body2 = upload(client, project, owner["headers"], cross, "part2.csv").json()
    assert body2["status"] == "partial"  # 批次行续录接受，重复样品行拒绝
    assert body2["rejected_rows"] == 1

    from app.database import connection
    assert connection().execute("SELECT COUNT(*) FROM botany_samples WHERE sample_code='S-CH-1'").fetchone()[0] == 1


def test_same_file_reupload_is_duplicate_and_not_accumulated(client, owner, project):
    content = make_csv(full_dataset_rows())
    first = upload(client, project, owner["headers"], content, "a.csv").json()
    assert first["status"] == "accepted"
    again = upload(client, project, owner["headers"], content, "a-renamed.csv").json()
    assert again["status"] == "duplicate"
    assert again["accepted_rows"] == 0

    imports = client.get(f"{base_url(project)}/imports", headers=owner["headers"]).json()["data"]
    assert len(imports) == 1  # 重传不产生新导入记录
    from app.database import connection
    assert connection().execute("SELECT COUNT(*) FROM botany_samples").fetchone()[0] == 6
    assert connection().execute("SELECT COUNT(*) FROM botany_identifications").fetchone()[0] == 7


def test_transaction_interrupt_rolls_back_everything(client, owner, project):
    content = make_csv(full_dataset_rows())
    from app.botany.service import BotanyService

    calls = {"n": 0}

    def boom(row):
        calls["n"] += 1
        if calls["n"] == 6:  # 已写入批次与若干样品后中断
            raise RuntimeError("模拟浮选记录设备掉线")

    service = BotanyService()
    with pytest.raises(Exception):
        service.import_csv(project["id"], owner["user"]["id"], "boom.csv", content, on_row=boom)

    from app.database import connection
    db = connection()
    assert db.execute("SELECT COUNT(*) FROM botany_imports").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM botany_batches").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM botany_samples").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM botany_rejections").fetchone()[0] == 0


# ---------- 删失、污染与统计 -----------------------------------------------

def test_censored_counts_density_and_detection(client, owner, project):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    resp = client.post(f"{base_url(project)}/comparisons?persist=false",
                       json={"seed": 42, "iterations": 300}, headers=owner["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["result"]

    setaria = next(t for t in data["taxa"] if t["mapped_code"] == "SETARIA")
    groups = setaria["groups"]
    # 古河道组：S-CH-1 精确 20/10L=2，S-CH-2 仅知上限 <3 保守记 0/5L
    paleo = groups["paleochannel"]
    assert paleo["sample_count"] == 2
    assert paleo["density_per_liter"] == pytest.approx(20 / 15)
    assert paleo["density_mean"] == pytest.approx((2.0 + 0.0) / 2)
    assert paleo["detection_rate"] == 1.0  # <3 仍算一次检出
    assert paleo["below_detections"] == 1
    # 灰坑组：nd 未检出
    pit = groups["pit"]
    assert pit["detection_rate"] == 0.0
    assert pit["nd_records"] == 2
    # 未知分类独立标记
    unknown = next(t for t in data["taxa"] if t["mapped_code"] == "UNKNOWN-GRASS")
    assert unknown["is_unknown"] is True


def test_pollution_excluded_by_default(client, owner, project):
    rows = full_dataset_rows()
    rows.append({"记录类型": "土样", "批次": "B01", "样品编号": "S-POLLUTED", "遗迹编号": "H2", "遗迹类型": "灰坑", "体积": "6", "污染": "是", "污染备注": "现代根系侵入"})
    rows.append({"记录类型": "组分", "样品编号": "S-POLLUTED", "浮选组分": "轻"})
    rows.append({"记录类型": "鉴定", "样品编号": "S-POLLUTED", "浮选组分": "轻", "分类编号": "SETARIA", "置信度": "高", "计数": "99"})
    upload(client, project, owner["headers"], make_csv(rows))

    clean = client.post(f"{base_url(project)}/comparisons?persist=false",
                        json={"seed": 7, "iterations": 200}, headers=owner["headers"]).json()["result"]
    pit_clean = next(t for t in clean["taxa"] if t["mapped_code"] == "SETARIA")["groups"]["pit"]
    assert pit_clean["sample_count"] == 2  # 污染样品被剔除

    incl = client.post(f"{base_url(project)}/comparisons?persist=false",
                       json={"seed": 7, "iterations": 200, "include_polluted": True}, headers=owner["headers"]).json()["result"]
    pit_all = next(t for t in incl["taxa"] if t["mapped_code"] == "SETARIA")["groups"]["pit"]
    assert pit_all["sample_count"] == 3


def test_fixed_seed_is_deterministic_and_persisted(client, owner, project, members):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    payload = {"seed": 20260925, "iterations": 500, "confidence": 0.95, "label": "固定种子版本"}
    r1 = client.post(f"{base_url(project)}/comparisons", json=payload, headers=owner["headers"])
    r2 = client.post(f"{base_url(project)}/comparisons", json=payload, headers=members["reviewer"]["headers"])
    assert r1.status_code == r2.status_code == 201
    v1, v2 = r1.json(), r2.json()
    assert v1["version_number"] == 1 and v2["version_number"] == 2
    assert v1["seed"] == 20260925 and v1["iterations"] == 500
    assert v1["algorithm_version"] == "botany-bootstrap-1.0.0"
    # 同种子同迭代：自助区间逐位一致
    assert v1["result"] == v2["result"]
    ci = v1["result"]["overall"]["groups"]["paleochannel"]["density_ci"]
    assert isinstance(ci, list) and ci[0] <= ci[1]

    # 复算
    recompute = client.post(f"{base_url(project)}/stat-versions/{v1['id']}/recompute", headers=members["viewer"]["headers"])
    assert recompute.status_code == 200
    assert recompute.json()["consistent"] is True

    # viewer 不能生成持久版本
    forbidden = client.post(f"{base_url(project)}/comparisons", json=payload, headers=members["viewer"]["headers"])
    assert forbidden.status_code == 403


def test_group_contrasts_structure(client, owner, project):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    data = client.post(f"{base_url(project)}/comparisons?persist=false",
                       json={"seed": 11, "iterations": 200}, headers=owner["headers"]).json()["result"]
    pairs = {(c["group_a"], c["group_b"]): c for c in data["overall"]["contrasts"]}
    assert ("dwelling", "pit") in pairs
    contrast = pairs[("dwelling", "pit")]
    assert contrast["density_diff_ci"][0] <= contrast["density_diff"] <= contrast["density_diff_ci"][1]


# ---------- 批次锁定 --------------------------------------------------------

def test_locked_batch_rejects_appends(client, owner, project):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    batch_id = client.get(f"{base_url(project)}/batches", headers=owner["headers"]).json()["data"][0]["id"]
    locked = client.post(f"{base_url(project)}/batches/{batch_id}/lock", headers=owner["headers"])
    assert locked.status_code == 200 and locked.json()["status"] == "locked"

    append = make_csv([
        {"记录类型": "批次", "批次": "B01"},
        {"记录类型": "土样", "批次": "B01", "样品编号": "S-NEW", "遗迹类型": "灰坑", "体积": "5"},
    ])
    body = upload(client, project, owner["headers"], append, "after-lock.csv").json()
    assert body["rejected_rows"] == 2  # 批次行与样品行均因锁定被拒
    assert body["status"] == "rejected"
    rej = client.get(f"{base_url(project)}/imports/{body['import_id']}/rejections", headers=owner["headers"]).content.decode()
    assert "batch_locked" in rej


# ---------- 分类口径 --------------------------------------------------------

def test_taxonomy_view_groups_counts(client, owner, project, members):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    payload = {
        "view_code": "CROP-VS-WILD", "label": "农作物/野生",
        "mappings": [
            {"taxon_code": "SETARIA", "mapped_code": "CROP_MILLET", "mapped_label": "栽培粟类"},
        ],
    }
    resp = client.post(f"{base_url(project)}/views", json=payload, headers=members["researcher"]["headers"])
    assert resp.status_code == 201
    view_id = resp.json()["id"]

    # recorder 不能建口径
    denied = client.post(f"{base_url(project)}/views", json={**payload, "view_code": "X"}, headers=members["recorder"]["headers"])
    assert denied.status_code == 403

    data = client.post(f"{base_url(project)}/comparisons?persist=false",
                       json={"seed": 3, "iterations": 200, "view_id": view_id}, headers=owner["headers"]).json()["result"]
    codes = {t["mapped_code"] for t in data["taxa"]}
    assert "CROP_MILLET" in codes and "SETARIA" not in codes


# ---------- 变更集：待审、批准、原始鉴定不变 ---------------------------------

def test_changeset_rename_merge_approval_creates_version_without_rewrite(client, owner, project, members):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    from app.database import connection

    before = connection().execute(
        "SELECT t.taxon_code, i.count_value, i.censor FROM botany_identifications i JOIN botany_taxa t ON t.id=i.taxon_id ORDER BY i.id"
    ).fetchall()
    original = [tuple(r) for r in before]

    payload = {
        "title": "粟与狗尾草合并", "rationale": "鉴定专家复核意见", "seed": 99, "iterations": 300,
        "items": [
            {"action": "rename", "taxon_code": "SETARIA", "new_code": "SETARIA_ITALICA", "new_name": "Setaria italica"},
        ],
    }
    proposed = client.post(f"{base_url(project)}/changesets", json=payload, headers=members["researcher"]["headers"])
    assert proposed.status_code == 201
    cs_id = proposed.json()["id"]
    assert proposed.json()["status"] == "pending"

    # researcher 不能批准；reviewer 可以
    forbidden = client.post(f"{base_url(project)}/changesets/{cs_id}/review", json={"decision": "approved", "note": "x"},
                            headers=members["researcher"]["headers"])
    assert forbidden.status_code == 403
    approved = client.post(f"{base_url(project)}/changesets/{cs_id}/review", json={"decision": "approved", "note": "同意更名"},
                           headers=members["reviewer"]["headers"])
    assert approved.status_code == 200 and approved.json()["status"] == "approved"

    # 批准生成了新统计版本且关联变更集
    versions = client.get(f"{base_url(project)}/stat-versions", headers=owner["headers"]).json()["data"]
    assert versions and versions[-1]["changeset_id"] == cs_id
    assert versions[-1]["seed"] == 99 and versions[-1]["iterations"] == 300

    # 原始鉴定行完全没有被改写
    after = connection().execute(
        "SELECT t.taxon_code, i.count_value, i.censor FROM botany_identifications i JOIN botany_taxa t ON t.id=i.taxon_id ORDER BY i.id"
    ).fetchall()
    assert [tuple(r) for r in after] == original
    # 原始分类名也未被改
    assert connection().execute("SELECT COUNT(*) FROM botany_taxa WHERE taxon_code='SETARIA'").fetchone()[0] == 1

    # 新视图把旧码映射到新码
    views = client.get(f"{base_url(project)}/views", headers=owner["headers"]).json()["data"]
    new_view = max(views, key=lambda v: v["id"])
    detail = client.get(f"{base_url(project)}/views/{new_view['id']}", headers=owner["headers"]).json()
    mapping = next(m for m in detail["mappings"] if m["taxon_code"] == "SETARIA")
    assert mapping["mapped_code"] == "SETARIA_ITALICA"

    # 不能重复审定
    again = client.post(f"{base_url(project)}/changesets/{cs_id}/review", json={"decision": "approved"},
                        headers=members["reviewer"]["headers"])
    assert again.status_code == 409


def test_changeset_merge_then_approve_combines_counts(client, owner, project, members):
    rows = full_dataset_rows()
    # 给灰坑补一条 SETARIA 精确计数，再引入一个待合并分类
    rows.append({"记录类型": "鉴定", "样品编号": "S-PIT-1", "浮选组分": "轻", "分类编号": "SETARIA_VIRIDIS", "学名": "Setaria viridis", "置信度": "中", "计数": "6"})
    upload(client, project, owner["headers"], make_csv(rows))

    payload = {
        "title": "狗尾草并入粟", "seed": 5, "iterations": 200,
        "items": [{"action": "merge", "taxon_code": "SETARIA_VIRIDIS", "target_taxon_code": "SETARIA", "note": "栽培与野生难以区分"}],
    }
    cs = client.post(f"{base_url(project)}/changesets", json=payload, headers=members["researcher"]["headers"]).json()
    approved = client.post(f"{base_url(project)}/changesets/{cs['id']}/review",
                           json={"decision": "approved"}, headers=owner["headers"]).json()
    assert approved["stat_version_id"]

    # 合并后视图中两个旧码都映射到 SETARIA
    views = client.get(f"{base_url(project)}/views", headers=owner["headers"]).json()["data"]
    view_id = max(v["id"] for v in views)
    data = client.post(f"{base_url(project)}/comparisons?persist=false",
                       json={"seed": 5, "iterations": 200, "view_id": view_id}, headers=owner["headers"]).json()["result"]
    setaria = next(t for t in data["taxa"] if t["mapped_code"] == "SETARIA")
    # 灰坑组合并口径密度 = (nd 保守 0 + 6) / 4L
    assert setaria["groups"]["pit"]["density_per_liter"] == pytest.approx(0.75)


def test_changeset_rejection(client, owner, project, members):
    upload(client, project, owner["headers"], make_csv(full_dataset_rows()))
    cs = client.post(f"{base_url(project)}/changesets",
                     json={"title": "误报更名", "items": [{"action": "rename", "taxon_code": "SETARIA", "new_code": "X"}]},
                     headers=members["researcher"]["headers"]).json()
    resp = client.post(f"{base_url(project)}/changesets/{cs['id']}/review",
                       json={"decision": "rejected", "note": "证据不足"}, headers=members["reviewer"]["headers"])
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert client.get(f"{base_url(project)}/stat-versions", headers=owner["headers"]).json()["data"] == []


# ---------- 离线 CLI 导入 ---------------------------------------------------

def test_cli_botany_import(tmp_path, client, owner, project):
    csv_path = tmp_path / "offline.csv"
    csv_path.write_bytes(make_csv(full_dataset_rows()))
    env = {**os.environ, "ARCHAEOLOGY_DATABASE_PATH": os.environ["ARCHAEOLOGY_DATABASE_PATH"]}
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "botany-import", "--project", "BOT", "--as", "owner", "--file", str(csv_path)],
        cwd="/workspace", env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    import json
    out = json.loads(proc.stdout)
    assert out["status"] == "accepted" and out["accepted_rows"] == 20

    # 再次导入相同文件 → duplicate，退出码仍为 0
    proc2 = subprocess.run(
        [sys.executable, "-m", "app.cli", "botany-import", "--project", "BOT", "--as", "owner", "--file", str(csv_path)],
        cwd="/workspace", env=env, capture_output=True, text=True,
    )
    assert proc2.returncode == 0
    assert json.loads(proc2.stdout)["status"] == "duplicate"

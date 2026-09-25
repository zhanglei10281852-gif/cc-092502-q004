"""CSV 批量导入：拒绝清单、幂等重传、已确认批次保护、事务回滚、角色控制、CLI。"""
from __future__ import annotations

import json

import pytest

from paleo_data import (
    BATCHES_CSV,
    FRACTIONS_CSV,
    IDENTIFICATIONS_CSV,
    SAMPLES_CSV,
    import_file,
    import_standard_dataset,
)


def test_samples_import_with_rejects_and_download(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    csv_text = """sample_code,context_type
S1,灰坑
S2,居址
S3,不存在的类型
,灰坑
S1,古河道
"""
    response = import_file(client, pid, headers, "samples", csv_text)
    assert response.status_code == 201
    body = response.json()
    assert body["accepted_rows"] == 2
    assert body["rejected_rows"] == 3  # 未知类型 / 缺样品号 / 文件内重复
    assert body["total_rows"] == 5

    download = client.get(f"/api/projects/{pid}/paleo/imports/{body['id']}/rejects", headers=headers)
    assert download.status_code == 200
    assert "text/csv" in download.headers["content-type"]
    assert "attachment" in download.headers["content-disposition"]
    lines = [line for line in download.text.strip().splitlines()]
    assert lines[0].startswith("row_number,error_code,error_message")
    assert len(lines) == 4
    assert "bad_context_type" in download.text
    assert "missing_sample_code" in download.text
    assert "duplicate_in_file" in download.text

    samples = client.get(f"/api/projects/{pid}/paleo/samples", headers=headers).json()["data"]
    assert [s["sample_code"] for s in samples] == ["S1", "S2"]
    assert samples[0]["context_type"] == "ash_pit"


def test_import_requires_project_role(client, paleo_project):
    pid = paleo_project["project"]["id"]
    viewer = paleo_project["members"]["viewer"]["headers"]
    response = import_file(client, pid, viewer, "samples", SAMPLES_CSV)
    assert response.status_code == 403
    outsider = client.post("/api/users", json={"username": "outsider", "display_name": "局外人", "password": "Outsider!23456"})
    login = client.post("/api/sessions", json={"username": "outsider", "password": "Outsider!23456"})
    response = import_file(client, pid, {"Authorization": f"Bearer {login.json()['token']}"}, "samples", SAMPLES_CSV)
    assert response.status_code == 403


def test_full_chain_and_reference_errors(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_standard_dataset(client, pid, headers)

    bad = import_file(client, pid, headers, "identifications",
                      "batch_code,fraction_type,taxon,count\nNOPE,light,小麦,3\nB1,nope,小麦,3\n")
    assert bad.status_code == 201
    assert bad.json()["rejected_rows"] == 2
    download = client.get(f"/api/projects/{pid}/paleo/imports/{bad.json()['id']}/rejects", headers=headers)
    assert "unknown_batch" in download.text
    assert "bad_fraction_type" in download.text

    batches = client.get(f"/api/projects/{pid}/paleo/batches", headers=headers).json()["data"]
    assert len(batches) == 4
    assert batches[0]["status"] == "draft"


def test_ambiguous_fraction_rejected(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,5\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type,mesh_size_mm\nB1,light,0.25\nB1,light,0.5\n")
    response = import_file(client, pid, headers, "identifications", "batch_code,fraction_type,taxon,count\nB1,light,小麦,3\n")
    assert response.json()["rejected_rows"] == 1
    download = client.get(f"/api/projects/{pid}/paleo/imports/{response.json()['id']}/rejects", headers=headers)
    assert "unknown_fraction" in download.text
    # 提供筛网孔径后可以唯一定位
    ok = import_file(client, pid, headers, "identifications",
                     "batch_code,fraction_type,mesh_size_mm,taxon,count\nB1,light,0.5,小麦,3\n")
    assert ok.json()["accepted_rows"] == 1


def test_idempotent_reimport_same_file(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_standard_dataset(client, pid, headers)
    first = import_file(client, pid, headers, "identifications", IDENTIFICATIONS_CSV, "identifications.csv")
    assert first.status_code == 201
    assert first.json()["idempotent_replay"] is True

    from app.database import connection
    count = connection().execute("SELECT COUNT(*) FROM bot_identifications").fetchone()[0]
    assert count == 5  # 重传同文件不会重复累加
    imports = client.get(f"/api/projects/{pid}/paleo/imports", headers=headers).json()["data"]
    assert len([i for i in imports if i["kind"] == "identifications"]) == 1


def test_confirmed_batch_survives_reimport(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_standard_dataset(client, pid, recorder)

    confirm = client.post(f"/api/projects/{pid}/paleo/batches/B1/confirm", headers=researcher)
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "confirmed"

    # 不同内容的新文件试图改写已确认批次：整行跳过，不重复累加
    changed_batches = "batch_code,sample_code,volume_liters\nB1,S1,999\nB5,S1,6\n"
    response = import_file(client, pid, recorder, "batches", changed_batches, "batches-v2.csv")
    assert response.json()["skipped_rows"] == 1
    assert response.json()["accepted_rows"] == 1
    import_file(client, pid, recorder, "fractions", "batch_code,fraction_type\nB5,light\n", "fractions-v2.csv")
    changed_idents = "batch_code,fraction_type,item_no,taxon,count\nB1,light,9,杂草,100\nB5,light,1,小麦,7\n"
    response = import_file(client, pid, recorder, "identifications", changed_idents, "idents-v2.csv")
    assert response.json()["skipped_rows"] == 1
    assert response.json()["accepted_rows"] == 1

    from app.database import connection
    volume = connection().execute("SELECT volume_liters FROM bot_batches WHERE batch_code='B1'").fetchone()[0]
    assert volume == 10  # 已确认批次未被改写
    count = connection().execute(
        "SELECT COUNT(*) FROM bot_identifications i JOIN bot_fractions f ON f.id=i.fraction_id"
        " JOIN bot_batches b ON b.id=f.batch_id WHERE b.batch_code='B1'"
    ).fetchone()[0]
    assert count == 1  # 已确认批次内鉴定未被追加


def test_transaction_interruption_rolls_back(client, paleo_project, monkeypatch):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS9,灰坑\n")

    from app.paleo.service import PaleoService
    original = PaleoService._write_sample
    calls = {"n": 0}

    def exploding(self, db, project_id, record, import_id, stamp, context):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟事务中断")
        return original(self, db, project_id, record, import_id, stamp, context)

    monkeypatch.setattr(PaleoService, "_write_sample", exploding)
    csv_text = "sample_code,context_type\nS10,灰坑\nS11,居址\nS12,古河道\n"
    with pytest.raises(RuntimeError, match="模拟事务中断"):
        import_file(client, pid, headers, "samples", csv_text, "broken.csv")

    from app.database import connection
    db = connection()
    codes = {row[0] for row in db.execute("SELECT sample_code FROM bot_samples").fetchall()}
    assert codes == {"S9"}  # 中断前写入的 S10 也一并回滚
    assert db.execute("SELECT COUNT(*) FROM bot_imports WHERE filename='broken.csv'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM bot_import_rejects").fetchone()[0] == 0


def test_zero_and_negative_volume_batches(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\nS2,灰坑\n")
    response = import_file(client, pid, headers, "batches",
                           "batch_code,sample_code,volume_liters\nB1,S1,0\nB2,S2,-3\n")
    assert response.json()["accepted_rows"] == 1  # 零体积允许导入（密度计算时排除）
    assert response.json()["rejected_rows"] == 1  # 负体积进入拒绝清单
    download = client.get(f"/api/projects/{pid}/paleo/imports/{response.json()['id']}/rejects", headers=headers)
    assert "negative_volume" in download.text


def test_normalization_variants(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_file(client, pid, headers, "samples", "样品号,遗迹类型\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "批次号,样品号,土样体积\nB1,S1,4\n")
    import_file(client, pid, headers, "fractions", "批次号,组分类型,筛网规格\nB1,轻浮物,0.25\n")
    idents = (
        "批次号,组分,序号,鉴定结果,数量,置信度,污染\n"
        "B1,轻浮,1,未知,,,\n"
        "B1,轻浮,2,小麦,<10,cf.,现代根系\n"
        "B1,轻浮,3,大麦,检出,中,\n"
        "B1,轻浮,4,黍,0,高,无\n"
    )
    response = import_file(client, pid, headers, "identifications", idents)
    assert response.json()["accepted_rows"] == 4, response.json()

    from app.database import connection
    rows = [tuple(row) for row in connection().execute(
        "SELECT taxon_normalized,count_type,count_max,is_unknown,contamination,confidence_level"
        " FROM bot_identifications ORDER BY item_no"
    ).fetchall()]
    assert rows[0] == ("unidentified", "present", None, 1, "", "")
    assert rows[1] == ("小麦", "upper", 10.0, 0, "modern_root", "low")
    assert rows[2] == ("大麦", "present", None, 0, "", "medium")
    assert rows[3] == ("黍", "absent", 0.0, 0, "", "high")


def test_duplicate_item_no_in_file_rejected(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,5\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type\nB1,light\n")
    response = import_file(client, pid, headers, "identifications",
                           "batch_code,fraction_type,item_no,taxon,count\n"
                           "B1,light,1,小麦,3\nB1,light,1,大麦,4\nB1,light,2,黍,5\n")
    body = response.json()
    assert body["accepted_rows"] == 2
    assert body["rejected_rows"] == 1
    download = client.get(f"/api/projects/{pid}/paleo/imports/{body['id']}/rejects", headers=headers)
    assert "duplicate_in_file" in download.text


def test_cli_paleo_import(client, paleo_project, monkeypatch, capsys, tmp_path):
    pid = paleo_project["project"]["id"]
    csv_path = tmp_path / "samples.csv"
    csv_path.write_text(SAMPLES_CSV, encoding="utf-8")
    from app.cli import main
    monkeypatch.setattr("sys.argv", ["cli", "paleo-import", "--project", "PALEO", "--kind", "samples",
                                     "--file", str(csv_path), "--user", "recorder"])
    assert main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["accepted_rows"] == 4
    assert out["rejected_rows"] == 0

    from app.database import connection
    count = connection().execute("SELECT COUNT(*) FROM bot_samples WHERE project_id=?", (pid,)).fetchone()[0]
    assert count == 4


def test_cli_paleo_import_denied_for_viewer(client, paleo_project, monkeypatch, capsys, tmp_path):
    csv_path = tmp_path / "samples.csv"
    csv_path.write_text(SAMPLES_CSV, encoding="utf-8")
    from app.cli import main
    monkeypatch.setattr("sys.argv", ["cli", "paleo-import", "--project", "PALEO", "--kind", "samples",
                                     "--file", str(csv_path), "--user", "viewer"])
    assert main() == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == "forbidden"

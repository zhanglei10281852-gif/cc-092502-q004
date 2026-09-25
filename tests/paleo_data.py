"""植物遗存测试的公共数据与导入辅助。"""
from __future__ import annotations

SAMPLES_CSV = """sample_code,context_type,context_label,volume_note
S1,灰坑,H1,备注
S2,灰坑,H2,
S3,居址,F1,
S4,古河道,G1,
"""

BATCHES_CSV = """batch_code,sample_code,volume_liters,operator
B1,S1,10,张三
B2,S2,10,张三
B3,S3,20,李四
B4,S4,8,李四
"""

FRACTIONS_CSV = """batch_code,fraction_type,mesh_size_mm
B1,light,0.25
B2,light,0.25
B3,light,0.25
B4,light,0.25
B1,heavy,1.0
"""

# B1: 小麦20粒；B2: 小麦10粒 + 大麦仅检出；B3: 小麦40粒；B4: 小麦<16（仅知上限）
IDENTIFICATIONS_CSV = """batch_code,fraction_type,item_no,taxon,family,genus,species,count,confidence,analyst
B1,light,1,小麦,禾本科,小麦属,普通小麦,20,高,王鉴定
B2,light,1,小麦,禾本科,小麦属,普通小麦,10,高,王鉴定
B2,light,2,大麦,禾本科,大麦属,大麦,present,中,王鉴定
B3,light,1,小麦,禾本科,小麦属,普通小麦,40,高,王鉴定
B4,light,1,小麦,禾本科,小麦属,普通小麦,<16,低,王鉴定
"""


def import_file(client, project_id: int, headers: dict, kind: str, content: str, filename: str = "data.csv"):
    return client.post(
        f"/api/projects/{project_id}/paleo/imports/{kind}",
        files={"file": (filename, content.encode("utf-8"), "text/csv")},
        headers=headers,
    )


def import_standard_dataset(client, project_id: int, headers: dict):
    """按 samples→batches→fractions→identifications 顺序导入标准数据集。"""
    for kind, content in [
        ("samples", SAMPLES_CSV),
        ("batches", BATCHES_CSV),
        ("fractions", FRACTIONS_CSV),
        ("identifications", IDENTIFICATIONS_CSV),
    ]:
        response = import_file(client, project_id, headers, kind, content, f"{kind}.csv")
        assert response.status_code == 201, response.text
        assert response.json()["rejected_rows"] == 0, response.json()

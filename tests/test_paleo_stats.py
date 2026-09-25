"""统计版本：每升密度、出现率、删失计数、零体积、固定随机种子、分组比较、分类口径。"""
from __future__ import annotations

import pytest

from paleo_data import import_file, import_standard_dataset


def _run(client, pid, headers, **params):
    response = client.post(f"/api/projects/{pid}/paleo/stats/runs", json=params, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _result(run, group, taxon):
    for row in run["results"]:
        if row["group_key"] == group and row["taxon_key"] == taxon:
            return row
    raise AssertionError(f"缺少结果行: {group}/{taxon}")


def test_density_and_ubiquity(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_standard_dataset(client, pid, recorder)

    run = _run(client, pid, researcher, rank="species", seed=7, iterations=500)
    assert run["run"]["version_no"] == 1
    assert run["run"]["algorithm_version"] == "paleo-stats/1.0.0"
    assert run["run"]["seed"] == 7
    assert run["run"]["iterations"] == 500

    # 灰坑：小麦 (20+10)/(10+10)=1.5 粒/升，出现率 2/2
    wheat_pit = _result(run, "ash_pit", "普通小麦")
    assert wheat_pit["density_point"] == pytest.approx(1.5)
    assert wheat_pit["density_min"] == pytest.approx(1.5)
    assert wheat_pit["ubiquity"] == pytest.approx(1.0)
    assert wheat_pit["n_samples"] == 2
    assert wheat_pit["density_ci_low"] <= wheat_pit["density_point"] <= wheat_pit["density_ci_high"]

    # 居址：小麦 40/20=2.0；大麦仅检出 → 计数为 0 但出现率 1/1
    wheat_dwelling = _result(run, "dwelling", "普通小麦")
    assert wheat_dwelling["density_point"] == pytest.approx(2.0)
    barley = _result(run, "ash_pit", "大麦")
    assert barley["count_max"] == 0
    assert barley["ubiquity"] == pytest.approx(0.5)

    # 古河道：小麦 <16 → 密度区间 [0, 2.0]，中点 1.0
    wheat_channel = _result(run, "paleochannel", "普通小麦")
    assert wheat_channel["count_min"] == 0
    assert wheat_channel["count_max"] == 16
    assert wheat_channel["density_min"] == 0
    assert wheat_channel["density_max"] == pytest.approx(2.0)
    assert wheat_channel["density_point"] == pytest.approx(1.0)

    # 检测限：每组最小可检密度 = 1/总体积，随版本保存
    groups = {g["group_key"]: g for g in run["run"]["groups"]}
    assert groups["ash_pit"]["min_detectable_density"] == pytest.approx(1 / 20)
    assert groups["dwelling"]["min_detectable_density"] == pytest.approx(1 / 20)
    assert groups["paleochannel"]["min_detectable_density"] == pytest.approx(1 / 8)


def test_group_comparison_with_bootstrap_ci(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_standard_dataset(client, pid, recorder)
    run = _run(client, pid, researcher, rank="species", seed=11, iterations=500)

    comparison = None
    for row in run["comparisons"]:
        if row["taxon_key"] == "普通小麦" and {row["group_a"], row["group_b"]} == {"ash_pit", "dwelling"}:
            comparison = row
    assert comparison is not None
    assert comparison["diff_point"] == pytest.approx(1.5 - 2.0)
    assert comparison["diff_ci_low"] <= comparison["diff_point"] <= comparison["diff_ci_high"]


def test_fixed_seed_reproducible(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    # 6 个样品、计数有梯度，使自助重采样统计量连续变化
    import_file(client, pid, headers, "samples",
                "sample_code,context_type\n" + "".join(f"S{i},灰坑\n" for i in range(1, 7)))
    import_file(client, pid, headers, "batches",
                "batch_code,sample_code,volume_liters\n" + "".join(f"B{i},S{i},10\n" for i in range(1, 7)))
    import_file(client, pid, headers, "fractions",
                "batch_code,fraction_type\n" + "".join(f"B{i},light\n" for i in range(1, 7)))
    counts = [3, 7, 12, 18, 25, 40]
    import_file(client, pid, headers, "identifications",
                "batch_code,fraction_type,item_no,taxon,count\n"
                + "".join(f"B{i},light,1,小麦,{c}\n" for i, c in enumerate(counts, start=1)))

    first = _run(client, pid, researcher, rank="species", seed=2026, iterations=800)
    second = _run(client, pid, researcher, rank="species", seed=2026, iterations=800)
    assert first["run"]["version_no"] != second["run"]["version_no"]

    def fingerprint(run):
        keys = ("density_point", "density_ci_low", "density_ci_high", "ubiquity_ci_low", "ubiquity_ci_high")
        num = lambda v: -1.0 if v is None else v
        results = sorted((r["group_key"], r["taxon_key"], *[num(r[k]) for k in keys]) for r in run["results"])
        comparisons = sorted((c["taxon_key"], c["group_a"], c["group_b"], num(c["diff_ci_low"]), num(c["diff_ci_high"]))
                             for c in run["comparisons"])
        return results, comparisons

    assert fingerprint(first) == fingerprint(second)  # 同种子同参数可复算

    third = _run(client, pid, researcher, rank="species", seed=999, iterations=800)
    assert fingerprint(first) != fingerprint(third)  # 不同种子产生不同重采样结果


def test_zero_volume_sample_excluded_from_density(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\nS2,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,0\nB2,S2,10\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type\nB1,light\nB2,light\n")
    import_file(client, pid, headers, "identifications",
                "batch_code,fraction_type,item_no,taxon,count\nB1,light,1,小麦,5\nB2,light,1,小麦,20\n")

    run = _run(client, pid, researcher, rank="species", seed=5, iterations=300)
    wheat = _result(run, "ash_pit", "小麦")
    # 零体积样品的 5 粒不计入密度：密度 = 20/10 = 2.0；出现率仍为 2/2
    assert wheat["density_point"] == pytest.approx(2.0)
    assert wheat["count_max"] == 20
    assert wheat["ubiquity"] == pytest.approx(1.0)
    assert wheat["n_samples"] == 2


def test_all_zero_volume_group_density_null(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,0\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type\nB1,light\n")
    import_file(client, pid, headers, "identifications", "batch_code,fraction_type,item_no,taxon,count\nB1,light,1,小麦,5\n")

    run = _run(client, pid, researcher, rank="species", seed=5, iterations=300)
    wheat = _result(run, "ash_pit", "小麦")
    assert wheat["density_point"] is None
    assert wheat["density_ci_low"] is None
    assert wheat["ubiquity"] == pytest.approx(1.0)  # 出现率不受零体积影响


def test_rank_selection_changes_grouping(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_standard_dataset(client, pid, recorder)

    family_run = _run(client, pid, researcher, rank="family", seed=3, iterations=300)
    taxa = {row["taxon_key"] for row in family_run["results"]}
    assert "禾本科" in taxa
    assert "普通小麦" not in taxa
    # 禾本科在灰坑：小麦20+10，大麦仅检出 → 密度 30/20
    poaceae = _result(family_run, "ash_pit", "禾本科")
    assert poaceae["density_point"] == pytest.approx(1.5)
    assert poaceae["ubiquity"] == pytest.approx(1.0)

    genus_run = _run(client, pid, researcher, rank="genus", seed=3, iterations=300)
    genus_taxa = {row["taxon_key"] for row in genus_run["results"]}
    assert "小麦属" in genus_taxa and "大麦属" in genus_taxa


def test_contaminated_rows_excluded_by_default(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,10\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type\nB1,light\n")
    import_file(client, pid, headers, "identifications",
                "batch_code,fraction_type,item_no,taxon,count,contamination\n"
                "B1,light,1,小麦,10,\nB1,light,2,现代杂草,50,现代根系\n")

    run = _run(client, pid, researcher, rank="species", seed=2, iterations=300)
    taxa = {row["taxon_key"] for row in run["results"]}
    assert "现代杂草" not in taxa  # 现代根系污染默认排除

    included = _run(client, pid, researcher, rank="species", seed=2, iterations=300, include_contaminated=True)
    taxa = {row["taxon_key"] for row in included["results"]}
    assert "现代杂草" in taxa


def test_unknown_taxon_reported_as_unidentified(client, paleo_project):
    pid = paleo_project["project"]["id"]
    headers = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    import_file(client, pid, headers, "samples", "sample_code,context_type\nS1,灰坑\n")
    import_file(client, pid, headers, "batches", "batch_code,sample_code,volume_liters\nB1,S1,4\n")
    import_file(client, pid, headers, "fractions", "batch_code,fraction_type\nB1,light\n")
    import_file(client, pid, headers, "identifications",
                "batch_code,fraction_type,item_no,taxon,count\nB1,light,1,未知,3\n")

    run = _run(client, pid, researcher, rank="species", seed=2, iterations=300)
    unknown = _result(run, "ash_pit", "unidentified")
    assert unknown["density_point"] == pytest.approx(0.75)


def test_stats_params_validated(client, paleo_project):
    pid = paleo_project["project"]["id"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    bad_rank = client.post(f"/api/projects/{pid}/paleo/stats/runs", json={"rank": "order"}, headers=researcher)
    assert bad_rank.status_code == 422
    bad_iter = client.post(f"/api/projects/{pid}/paleo/stats/runs", json={"iterations": 10}, headers=researcher)
    assert bad_iter.status_code == 422


def test_stats_read_roles(client, paleo_project):
    pid = paleo_project["project"]["id"]
    recorder = paleo_project["members"]["recorder"]["headers"]
    researcher = paleo_project["members"]["researcher"]["headers"]
    viewer = paleo_project["members"]["viewer"]["headers"]
    import_standard_dataset(client, pid, recorder)
    run = _run(client, pid, researcher, rank="species", seed=1, iterations=200)
    run_id = run["run"]["id"]

    assert client.get(f"/api/projects/{pid}/paleo/stats/runs", headers=viewer).status_code == 200
    assert client.get(f"/api/projects/{pid}/paleo/stats/runs/{run_id}", headers=viewer).status_code == 200
    # 记录员与访客不能创建统计版本
    assert client.post(f"/api/projects/{pid}/paleo/stats/runs", json={}, headers=recorder).status_code == 403
    assert client.post(f"/api/projects/{pid}/paleo/stats/runs", json={}, headers=viewer).status_code == 403

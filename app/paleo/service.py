"""植物遗存模块的业务服务：CSV 导入、批次确认、统计版本、鉴定变更集。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.paleo import csvio, stats
from app.paleo.csvio import FileError, IMPORT_KINDS, mesh_key, normalize_taxon
from app.security import stable_json
from app.service import ResearchService, ServiceError

IMPORT_ROLES = {"owner", "researcher", "recorder"}
CONFIRM_ROLES = {"owner", "researcher"}
STATS_RUN_ROLES = {"owner", "researcher"}
CHANGESET_PROPOSE_ROLES = {"owner", "researcher"}
CHANGESET_REVIEW_ROLES = {"owner", "reviewer"}
READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}


class PaleoService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.core = ResearchService(self.db)

    # ---------- 通用 ----------

    def require(self, project_id: int, user_id: int, roles: set[str]) -> str:
        return self.core.require_role(project_id, user_id, roles)

    def _project_exists(self, project_id: int) -> None:
        row = self.db.execute("SELECT id FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ServiceError("project_not_found", "项目不存在", 404)

    # ---------- CSV 导入 ----------

    def import_csv(self, project_id: int, kind: str, filename: str, content: bytes, actor_id: int) -> dict[str, Any]:
        """解析并写入一个 CSV 文件。

        幂等：同一项目同一类别且文件哈希相同的重复上传直接返回首次导入结果，
        不会重复累加；已确认批次中的行被跳过。全部写入在单个即时事务中完成，
        中途失败整体回滚。
        """
        if kind not in IMPORT_KINDS:
            raise ServiceError("bad_kind", f"未知的导入类别: {kind}", 422)
        self._project_exists(project_id)
        digest = hashlib.sha256(content).hexdigest()
        existing = self.db.execute(
            "SELECT * FROM bot_imports WHERE project_id=? AND kind=? AND file_sha256=?",
            (project_id, kind, digest),
        ).fetchone()
        if existing is not None:
            return self._import_payload(existing, idempotent=True)
        try:
            parsed = csvio.parse_file(kind, content)
        except FileError as exc:
            raise ServiceError(exc.code, exc.message, 422) from exc

        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO bot_imports(project_id,kind,filename,file_sha256,status,total_rows,actor_id,created_at)"
                    " VALUES(?,?,?,?,'done',?,?,?)",
                    (project_id, kind, filename, digest, len(parsed.records) + len(parsed.rejects), actor_id, stamp),
                )
                import_id = cursor.lastrowid
                accepted, skipped, db_rejects = self._apply_records(db, project_id, kind, parsed.records, import_id, stamp)
                all_rejects = list(parsed.rejects) + db_rejects
                for reject in all_rejects:
                    db.execute(
                        "INSERT INTO bot_import_rejects(import_id,row_number,raw_json,error_code,error_message,created_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (import_id, reject["row_number"], stable_json(reject["raw"]), reject["error_code"], reject["error_message"], stamp),
                    )
                db.execute(
                    "UPDATE bot_imports SET accepted_rows=?,rejected_rows=?,skipped_rows=? WHERE id=?",
                    (accepted, len(all_rejects), skipped, import_id),
                )
                self.core.audit(
                    "paleo.import", "import", str(import_id),
                    {"kind": kind, "filename": filename, "sha256": digest, "accepted": accepted,
                     "rejected": len(all_rejects), "skipped": skipped},
                    project_id=project_id, actor_id=actor_id,
                )
        except sqlite3.IntegrityError:
            # 并发重复上传同一文件：返回已存在的导入记录，保证幂等
            existing = self.db.execute(
                "SELECT * FROM bot_imports WHERE project_id=? AND kind=? AND file_sha256=?",
                (project_id, kind, digest),
            ).fetchone()
            if existing is not None:
                return self._import_payload(existing, idempotent=True)
            raise
        row = self.db.execute("SELECT * FROM bot_imports WHERE id=?", (import_id,)).fetchone()
        return self._import_payload(row, idempotent=False)

    def _import_payload(self, row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["idempotent_replay"] = idempotent
        return payload

    def _apply_records(self, db: sqlite3.Connection, project_id: int, kind: str,
                       records: list[dict[str, Any]], import_id: int, stamp: str) -> tuple[int, int, list[dict[str, Any]]]:
        writers = {
            "samples": self._write_sample,
            "batches": self._write_batch,
            "fractions": self._write_fraction,
            "identifications": self._write_identification,
        }
        accepted = skipped = 0
        db_rejects: list[dict[str, Any]] = []
        context = {"seen": set(), "auto_items": {}}
        for record in records:
            try:
                outcome = writers[kind](db, project_id, record, import_id, stamp, context)
            except _SkipAsReject as exc:
                db_rejects.append({
                    "row_number": record.get("_row_number", 0),
                    "raw": record.get("_raw", {}),
                    "error_code": exc.code,
                    "error_message": exc.message,
                })
                continue
            if outcome == "skipped":
                skipped += 1
            else:
                accepted += 1
        return accepted, skipped, db_rejects

    def _duplicate_in_file(self, context: dict[str, Any], key: tuple) -> bool:
        seen = context["seen"]
        if key in seen:
            return True
        seen.add(key)
        return False

    def _write_sample(self, db, project_id, record, import_id, stamp, context) -> str:
        if self._duplicate_in_file(context, (record["sample_code"],)):
            raise _SkipAsReject(record, "duplicate_in_file", "同一文件中样品号重复")
        db.execute(
            "INSERT INTO bot_samples(project_id,sample_code,context_type,context_label,collected_at,notes,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id,sample_code) DO UPDATE SET context_type=excluded.context_type,"
            " context_label=excluded.context_label,collected_at=excluded.collected_at,notes=excluded.notes,updated_at=excluded.updated_at",
            (project_id, record["sample_code"], record["context_type"], record["context_label"],
             record["collected_at"], record["notes"], stamp, stamp),
        )
        return "accepted"

    def _find_sample(self, db, project_id: int, sample_code: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM bot_samples WHERE project_id=? AND sample_code=?", (project_id, sample_code)
        ).fetchone()

    def _find_batch(self, db, project_id: int, batch_code: str) -> sqlite3.Row | None:
        return db.execute(
            "SELECT * FROM bot_batches WHERE project_id=? AND batch_code=?", (project_id, batch_code)
        ).fetchone()

    def _write_batch(self, db, project_id, record, import_id, stamp, context) -> str:
        if self._duplicate_in_file(context, (record["batch_code"],)):
            raise _SkipAsReject(record, "duplicate_in_file", "同一文件中批次号重复")
        sample = self._find_sample(db, project_id, record["sample_code"])
        if sample is None:
            raise _SkipAsReject(record, "unknown_sample", f"样品不存在: {record['sample_code']}")
        existing = self._find_batch(db, project_id, record["batch_code"])
        if existing is not None and existing["status"] == "confirmed":
            return "skipped"  # 已确认批次不被重传改写
        db.execute(
            "INSERT INTO bot_batches(project_id,batch_code,sample_id,volume_liters,floated_at,operator,notes,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(project_id,batch_code) DO UPDATE SET sample_id=excluded.sample_id,"
            " volume_liters=excluded.volume_liters,floated_at=excluded.floated_at,operator=excluded.operator,"
            " notes=excluded.notes,updated_at=excluded.updated_at",
            (project_id, record["batch_code"], sample["id"], record["volume_liters"],
             record["floated_at"], record["operator"], record["notes"], stamp, stamp),
        )
        return "accepted"

    def _write_fraction(self, db, project_id, record, import_id, stamp, context) -> str:
        key = (record["batch_code"], record["fraction_type"], mesh_key(record["mesh_size_mm"]))
        if self._duplicate_in_file(context, key):
            raise _SkipAsReject(record, "duplicate_in_file", "同一文件中组分重复")
        batch = self._find_batch(db, project_id, record["batch_code"])
        if batch is None:
            raise _SkipAsReject(record, "unknown_batch", f"浮选批次不存在: {record['batch_code']}")
        if batch["status"] == "confirmed":
            return "skipped"
        db.execute(
            "INSERT INTO bot_fractions(project_id,batch_id,fraction_type,mesh_size_mm,mesh_key,notes,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(batch_id,fraction_type,mesh_key) DO UPDATE SET notes=excluded.notes,updated_at=excluded.updated_at",
            (project_id, batch["id"], record["fraction_type"], record["mesh_size_mm"],
             mesh_key(record["mesh_size_mm"]), record["notes"], stamp, stamp),
        )
        return "accepted"

    def _resolve_fraction(self, db, project_id: int, record: dict[str, Any]) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str]:
        batch = self._find_batch(db, project_id, record["batch_code"])
        if batch is None:
            return None, None, f"浮选批次不存在: {record['batch_code']}"
        candidates = db.execute(
            "SELECT * FROM bot_fractions WHERE batch_id=? AND fraction_type=?",
            (batch["id"], record["fraction_type"]),
        ).fetchall()
        if record["mesh_size_mm"] is not None:
            candidates = [c for c in candidates if c["mesh_key"] == mesh_key(record["mesh_size_mm"])]
        if not candidates:
            return batch, None, f"组分不存在: {record['batch_code']} / {record['fraction_type']}"
        if len(candidates) > 1:
            return batch, None, f"组分不唯一，请提供筛网孔径: {record['batch_code']} / {record['fraction_type']}"
        return batch, candidates[0], ""

    def _write_identification(self, db, project_id, record, import_id, stamp, context) -> str:
        batch, fraction, error = self._resolve_fraction(db, project_id, record)
        if fraction is None:
            code = "unknown_batch" if batch is None else "unknown_fraction"
            raise _SkipAsReject(record, code, error)
        if batch["status"] == "confirmed":
            return "skipped"
        item_no = record["item_no"]
        if item_no is None:
            item_no = context["auto_items"].get(fraction["id"], 0) + 1
            context["auto_items"][fraction["id"]] = item_no
        if self._duplicate_in_file(context, ("ident", fraction["id"], item_no)):
            raise _SkipAsReject(record, "duplicate_in_file", f"同一文件中鉴定序号重复: {item_no}")
        db.execute(
            "INSERT INTO bot_identifications(project_id,fraction_id,item_no,taxon_raw,taxon_normalized,family,genus,species,"
            " count_type,count_value,count_max,is_unknown,contamination,confidence_raw,confidence_level,analyst,notes,import_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fraction_id,item_no) DO UPDATE SET taxon_raw=excluded.taxon_raw,"
            " taxon_normalized=excluded.taxon_normalized,family=excluded.family,genus=excluded.genus,species=excluded.species,"
            " count_type=excluded.count_type,count_value=excluded.count_value,count_max=excluded.count_max,"
            " is_unknown=excluded.is_unknown,contamination=excluded.contamination,confidence_raw=excluded.confidence_raw,"
            " confidence_level=excluded.confidence_level,analyst=excluded.analyst,notes=excluded.notes,"
            " import_id=excluded.import_id,updated_at=excluded.updated_at",
            (project_id, fraction["id"], item_no, record["taxon_raw"], record["taxon_normalized"],
             record["family"], record["genus"], record["species"], record["count_type"],
             record["count_value"], record["count_max"], record["is_unknown"], record["contamination"],
             record["confidence_raw"], record["confidence_level"], record["analyst"], record["notes"],
             import_id, stamp, stamp),
        )
        return "accepted"

    # ---------- 查询 ----------

    def list_imports(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM bot_imports WHERE project_id=? ORDER BY id", (project_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_import(self, project_id: int, import_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM bot_imports WHERE project_id=? AND id=?", (project_id, import_id)
        ).fetchone()
        if row is None:
            raise ServiceError("import_not_found", "导入记录不存在", 404)
        return dict(row)

    def import_rejects_csv(self, project_id: int, import_id: int) -> str:
        self.get_import(project_id, import_id)
        rows = self.db.execute(
            "SELECT * FROM bot_import_rejects WHERE import_id=? ORDER BY row_number,id", (import_id,)
        ).fetchall()
        import csv
        import io
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["row_number", "error_code", "error_message", "raw_json"])
        for row in rows:
            writer.writerow([row["row_number"], row["error_code"], row["error_message"], row["raw_json"]])
        return buffer.getvalue()

    def list_samples(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM bot_samples WHERE project_id=? ORDER BY sample_code", (project_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_batches(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT b.*, s.sample_code FROM bot_batches b JOIN bot_samples s ON s.id=b.sample_id"
            " WHERE b.project_id=? ORDER BY b.batch_code", (project_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 批次确认 ----------

    def confirm_batch(self, project_id: int, batch_code: str, actor_id: int) -> dict[str, Any]:
        stamp = now()
        with transaction(immediate=True) as db:
            batch = self._find_batch(db, project_id, batch_code)
            if batch is None:
                raise ServiceError("batch_not_found", "浮选批次不存在", 404)
            if batch["status"] == "confirmed":
                return dict(batch)
            db.execute(
                "UPDATE bot_batches SET status='confirmed',confirmed_by=?,confirmed_at=?,updated_at=? WHERE id=?",
                (actor_id, stamp, stamp, batch["id"]),
            )
            self.core.audit("paleo.batch.confirm", "batch", str(batch["id"]), {"batch_code": batch_code},
                            project_id=project_id, actor_id=actor_id)
            return dict(db.execute("SELECT * FROM bot_batches WHERE id=?", (batch["id"],)).fetchone())

    # ---------- 统计版本 ----------

    def _load_samples(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT s.id AS sample_id, s.sample_code, s.context_type,"
            " COALESCE(SUM(b.volume_liters),0) AS volume_liters"
            " FROM bot_samples s JOIN bot_batches b ON b.sample_id=s.id"
            " WHERE s.project_id=? GROUP BY s.id",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _load_rows(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT s.id AS sample_id, i.taxon_normalized, i.family, i.genus, i.species,"
            " i.count_type, i.count_value, i.count_max, i.is_unknown, i.contamination, i.confidence_level"
            " FROM bot_identifications i"
            " JOIN bot_fractions f ON f.id=i.fraction_id"
            " JOIN bot_batches b ON b.id=f.batch_id"
            " JOIN bot_samples s ON s.id=b.sample_id"
            " WHERE i.project_id=?",
            (project_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _approved_taxon_map(self, project_id: int) -> tuple[dict[str, str], list[int]]:
        rows = self.db.execute(
            "SELECT o.op_type, o.from_taxa_json, o.to_taxon, o.changeset_id"
            " FROM bot_changeset_ops o JOIN bot_changesets c ON c.id=o.changeset_id"
            " WHERE c.project_id=? AND c.status='approved' ORDER BY c.id, o.id",
            (project_id,),
        ).fetchall()
        ops = [{
            "op_type": row["op_type"],
            "from_taxa": json.loads(row["from_taxa_json"]),
            "to_taxon": row["to_taxon"],
        } for row in rows]
        return stats.build_taxon_map(ops), sorted({row["changeset_id"] for row in rows})

    def validate_stats_params(self, payload: dict[str, Any]) -> dict[str, Any]:
        rank = payload.get("rank", "species")
        if rank not in stats.RANKS:
            raise ServiceError("bad_rank", f"分类口径必须是 {list(stats.RANKS)} 之一", 422)
        seed = payload.get("seed", stats.DEFAULT_SEED)
        iterations = payload.get("iterations", stats.DEFAULT_ITERATIONS)
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ServiceError("bad_seed", "随机种子必须为整数", 422)
        if not isinstance(iterations, int) or isinstance(iterations, bool) \
                or not stats.MIN_ITERATIONS <= iterations <= stats.MAX_ITERATIONS:
            raise ServiceError("bad_iterations", f"迭代次数须位于 {stats.MIN_ITERATIONS}..{stats.MAX_ITERATIONS}", 422)
        min_confidence = payload.get("min_confidence") or ""
        if min_confidence not in ("", "high", "medium", "low"):
            raise ServiceError("bad_confidence", "min_confidence 必须是 high/medium/low", 422)
        return {
            "rank": rank,
            "seed": seed,
            "iterations": iterations,
            "include_contaminated": bool(payload.get("include_contaminated", False)),
            "min_confidence": min_confidence,
        }

    def create_stats_run(self, project_id: int, payload: dict[str, Any], actor_id: int,
                         *, changeset_id: int | None = None) -> dict[str, Any]:
        params = self.validate_stats_params(payload)
        samples = self._load_samples(project_id)
        rows = self._load_rows(project_id)
        taxon_map, applied_changesets = self._approved_taxon_map(project_id)
        computed = stats.compute_statistics(
            samples, rows,
            rank=params["rank"], seed=params["seed"], iterations=params["iterations"],
            params=params, taxon_map=taxon_map,
        )
        stamp = now()
        stored_params = dict(params)
        stored_params["applied_changeset_ids"] = applied_changesets
        with transaction(immediate=True) as db:
            version = db.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 AS v FROM bot_stats_runs WHERE project_id=?", (project_id,)
            ).fetchone()["v"]
            cursor = db.execute(
                "INSERT INTO bot_stats_runs(project_id,version_no,algorithm_version,rank,seed,iterations,params_json,"
                " groups_json,changeset_id,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'done',?,?)",
                (project_id, version, stats.ALGORITHM_VERSION, params["rank"], params["seed"],
                 params["iterations"], stable_json(stored_params), stable_json(computed["groups"]),
                 changeset_id, actor_id, stamp),
            )
            run_id = cursor.lastrowid
            for result in computed["results"]:
                db.execute(
                    "INSERT INTO bot_stats_results(run_id,group_key,taxon_key,n_samples,n_present,total_volume_liters,"
                    " count_min,count_max,density_min,density_max,density_point,density_ci_low,density_ci_high,"
                    " ubiquity,ubiquity_ci_low,ubiquity_ci_high) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, result["group_key"], result["taxon_key"], result["n_samples"], result["n_present"],
                     result["total_volume_liters"], result["count_min"], result["count_max"],
                     result["density_min"], result["density_max"], result["density_point"],
                     result["density_ci_low"], result["density_ci_high"], result["ubiquity"],
                     result["ubiquity_ci_low"], result["ubiquity_ci_high"]),
                )
            for comparison in computed["comparisons"]:
                db.execute(
                    "INSERT INTO bot_stats_comparisons(run_id,taxon_key,group_a,group_b,diff_point,diff_ci_low,diff_ci_high)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (run_id, comparison["taxon_key"], comparison["group_a"], comparison["group_b"],
                     comparison["diff_point"], comparison["diff_ci_low"], comparison["diff_ci_high"]),
                )
            self.core.audit("paleo.stats.run", "stats_run", str(run_id),
                            {"version_no": version, "params": stored_params},
                            project_id=project_id, actor_id=actor_id)
        return self.get_stats_run(project_id, run_id)

    def _run_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["params"] = json.loads(payload["params_json"])
        payload["groups"] = json.loads(payload["groups_json"])
        del payload["params_json"]
        del payload["groups_json"]
        return payload

    def get_stats_run(self, project_id: int, run_id: int) -> dict[str, Any]:
        run = self.db.execute(
            "SELECT * FROM bot_stats_runs WHERE project_id=? AND id=?", (project_id, run_id)
        ).fetchone()
        if run is None:
            raise ServiceError("run_not_found", "统计版本不存在", 404)
        results = self.db.execute(
            "SELECT * FROM bot_stats_results WHERE run_id=? ORDER BY group_key,taxon_key", (run_id,)
        ).fetchall()
        comparisons = self.db.execute(
            "SELECT * FROM bot_stats_comparisons WHERE run_id=? ORDER BY taxon_key,group_a,group_b", (run_id,)
        ).fetchall()
        return {
            "run": self._run_payload(run),
            "results": [dict(row) for row in results],
            "comparisons": [dict(row) for row in comparisons],
        }

    def list_stats_runs(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM bot_stats_runs WHERE project_id=? ORDER BY version_no", (project_id,)
        ).fetchall()
        return [self._run_payload(row) for row in rows]

    # ---------- 鉴定变更集 ----------

    def propose_changeset(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        ops = payload.get("ops") or []
        if not ops:
            raise ServiceError("empty_changeset", "变更集至少包含一条操作", 422)
        normalized_ops = []
        for op in ops:
            op_type = op.get("op")
            if op_type not in ("rename", "merge"):
                raise ServiceError("bad_op", "操作类型必须是 rename 或 merge", 422)
            to_taxon = normalize_taxon(str(op.get("to") or ""))
            if not to_taxon:
                raise ServiceError("bad_op", "目标分类名不能为空", 422)
            raw_from = op.get("from")
            if op_type == "rename":
                sources = list(raw_from) if isinstance(raw_from, list) else [raw_from]
                if len(sources) != 1:
                    raise ServiceError("bad_op", "更名操作只接受一个来源分类", 422)
            else:
                sources = list(raw_from) if isinstance(raw_from, list) else ([raw_from] if raw_from else [])
            sources = [normalize_taxon(str(s)) for s in sources if s and str(s).strip()]
            if not sources:
                raise ServiceError("bad_op", "来源分类名不能为空", 422)
            if to_taxon in sources:
                raise ServiceError("bad_op", "目标分类名不能与来源相同", 422)
            if op_type == "merge" and len(sources) < 2:
                raise ServiceError("bad_op", "合并操作至少需要两个来源分类", 422)
            normalized_ops.append({"op_type": op_type, "from_taxa": sources, "to_taxon": to_taxon})
        stats_params = payload.get("stats_params") or {}
        if stats_params:
            self.validate_stats_params(stats_params)
        stamp = now()
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO bot_changesets(project_id,status,note,stats_params_json,proposed_by,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (project_id, "pending", payload.get("note", ""), stable_json(stats_params), actor_id, stamp),
            )
            changeset_id = cursor.lastrowid
            for op in normalized_ops:
                db.execute(
                    "INSERT INTO bot_changeset_ops(changeset_id,op_type,from_taxa_json,to_taxon,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (changeset_id, op["op_type"], stable_json(op["from_taxa"]), op["to_taxon"], stamp),
                )
            self.core.audit("paleo.changeset.propose", "changeset", str(changeset_id),
                            {"ops": normalized_ops, "note": payload.get("note", "")},
                            project_id=project_id, actor_id=actor_id)
        return self.get_changeset(project_id, changeset_id)

    def get_changeset(self, project_id: int, changeset_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM bot_changesets WHERE project_id=? AND id=?", (project_id, changeset_id)
        ).fetchone()
        if row is None:
            raise ServiceError("changeset_not_found", "变更集不存在", 404)
        ops = self.db.execute(
            "SELECT op_type,from_taxa_json,to_taxon FROM bot_changeset_ops WHERE changeset_id=? ORDER BY id",
            (changeset_id,),
        ).fetchall()
        payload = dict(row)
        payload["stats_params"] = json.loads(payload["stats_params_json"])
        del payload["stats_params_json"]
        payload["ops"] = [
            {"op": op["op_type"], "from": json.loads(op["from_taxa_json"]), "to": op["to_taxon"]} for op in ops
        ]
        return payload

    def list_changesets(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id FROM bot_changesets WHERE project_id=? ORDER BY id", (project_id,)
        ).fetchall()
        return [self.get_changeset(project_id, row["id"]) for row in rows]

    def _pending_changeset(self, db, project_id: int, changeset_id: int) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM bot_changesets WHERE project_id=? AND id=?", (project_id, changeset_id)
        ).fetchone()
        if row is None:
            raise ServiceError("changeset_not_found", "变更集不存在", 404)
        if row["status"] != "pending":
            raise ServiceError("changeset_closed", "变更集已处理，不能重复操作", 409)
        return row

    def approve_changeset(self, project_id: int, changeset_id: int, actor_id: int,
                          review_note: str = "") -> dict[str, Any]:
        stamp = now()
        with transaction(immediate=True) as db:
            row = self._pending_changeset(db, project_id, changeset_id)
            if row["proposed_by"] == actor_id:
                raise ServiceError("self_approval", "变更集必须由提案人以外的成员批准", 409)
            db.execute(
                "UPDATE bot_changesets SET status='approved',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                (actor_id, stamp, review_note, changeset_id),
            )
            self.core.audit("paleo.changeset.approve", "changeset", str(changeset_id),
                            {"review_note": review_note}, project_id=project_id, actor_id=actor_id)
        # 批准后基于变更集自带参数（或最近版本参数，或默认参数）生成新统计版本；
        # 原始鉴定行不被改写，映射只在统计时应用。
        changeset = self.get_changeset(project_id, changeset_id)
        params = changeset["stats_params"]
        if not params:
            latest = self.db.execute(
                "SELECT params_json FROM bot_stats_runs WHERE project_id=? ORDER BY version_no DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            params = json.loads(latest["params_json"]) if latest else {}
            params.pop("applied_changeset_ids", None)
        run = self.create_stats_run(project_id, params, actor_id, changeset_id=changeset_id)
        return {"changeset": changeset, "stats_run": run}

    def reject_changeset(self, project_id: int, changeset_id: int, actor_id: int,
                         review_note: str = "") -> dict[str, Any]:
        stamp = now()
        with transaction(immediate=True) as db:
            self._pending_changeset(db, project_id, changeset_id)
            db.execute(
                "UPDATE bot_changesets SET status='rejected',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
                (actor_id, stamp, review_note, changeset_id),
            )
            self.core.audit("paleo.changeset.reject", "changeset", str(changeset_id),
                            {"review_note": review_note}, project_id=project_id, actor_id=actor_id)
        return self.get_changeset(project_id, changeset_id)


class _SkipAsReject(Exception):
    """导入过程中单行转为拒绝行的内部信号（由 _apply_records 上层的调用方捕获）。"""

    def __init__(self, record: dict[str, Any], code: str, message: str):
        self.record, self.code, self.message = record, code, message
        super().__init__(message)

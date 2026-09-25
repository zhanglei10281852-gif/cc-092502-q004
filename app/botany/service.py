"""植物考古领域服务：CSV 导入、批次锁定、统计版本与分类变更集。

所有跨表写入都包在单个 IMMEDIATE 事务中：单行数据错误进入拒绝清单并随事务
提交；任何意外异常则整体回滚，保证不会留下半个批次。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any, Callable, Iterable

from app.botany import csvio, stats
from app.botany.tables import create_schema
from app.database import connection, now, transaction
from app.security import stable_json
from app.service import ResearchService, ServiceError

EDIT_ROLES = {"owner", "researcher", "recorder"}
EXPERT_ROLES = {"owner", "researcher", "reviewer"}
APPROVE_ROLES = {"owner", "reviewer"}
READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}


class BotanyService:
    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()
        self.foundation = ResearchService(self.db)

    # ---- 基础引导 ----------------------------------------------------------

    def init_schema(self) -> None:
        create_schema(self.db)

    def _audit(self, action: str, resource_type: str, resource_id: str, payload: dict[str, Any], *, project_id: int, actor_id: int | None) -> None:
        self.foundation.audit(action, resource_type, resource_id, payload, project_id=project_id, actor_id=actor_id)

    def _project_or_404(self, project_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ServiceError("project_not_found", "项目不存在", 404)
        return row

    # ---- CSV 导入 ----------------------------------------------------------

    def import_csv(
        self,
        project_id: int,
        actor_id: int,
        filename: str,
        content: bytes,
        *,
        on_row: Callable[[csvio.RawRow], None] | None = None,
    ) -> dict[str, Any]:
        """导入一个植物考古 CSV 文件。

        同一项目下内容哈希相同的文件视为重传：直接返回 duplicate，不再累加任何数据。
        """
        self._project_or_404(project_id)
        self.foundation.require_role(project_id, actor_id, EDIT_ROLES)
        digest = hashlib.sha256(content).hexdigest()

        try:
            sheet = csvio.parse_sheet(content)
        except csvio.RowError as exc:
            raise ServiceError(exc.code, str(exc), 400) from exc

        previous = self.db.execute(
            "SELECT id,status,accepted_rows,rejected_rows FROM botany_imports WHERE project_id=? AND content_sha256=? ORDER BY id",
            (project_id, digest),
        ).fetchall()
        if previous:
            first = previous[0]
            self._audit("botany.import.duplicate", "botany_import", str(first["id"]), {"filename": filename, "sha256": digest}, project_id=project_id, actor_id=actor_id)
            return {
                "status": "duplicate",
                "import_id": first["id"],
                "filename": filename,
                "content_sha256": digest,
                "accepted_rows": 0,
                "rejected_rows": 0,
                "duplicate_lines": 0,
                "message": "同一文件已成功导入过，本次重传未产生新数据",
            }

        stamp = now()
        try:
            with transaction(immediate=True) as db:
                try:
                    cur = db.execute(
                        "INSERT INTO botany_imports(project_id,filename,content_sha256,status,created_at,imported_by) VALUES(?,?,?,?,?,?)",
                        (project_id, filename, digest, "accepted", stamp, actor_id),
                    )
                except sqlite3.IntegrityError as exc:
                    # 并发重传同一文件：唯一索引兜底
                    raise ServiceError("duplicate_import", "同一文件正在或已经导入", 409) from exc
                import_id = cur.lastrowid
                outcome = self._process_rows(db, project_id, actor_id, import_id, sheet, stamp, on_row=on_row)

                status = "accepted"
                if outcome["accepted"] == 0:
                    status = "rejected"
                elif outcome["rejected"]:
                    status = "partial"
                db.execute(
                    "UPDATE botany_imports SET status=?,accepted_rows=?,rejected_rows=?,duplicate_lines=?,summary_json=? WHERE id=?",
                    (status, outcome["accepted"], outcome["rejected"], outcome["duplicates"], stable_json(outcome["summary"]), import_id),
                )
                for batch_id in set(outcome["batch_samples"]) | set(outcome["batch_rejections"]):
                    db.execute(
                        "UPDATE botany_batches "
                        "SET samples_imported=(SELECT COUNT(*) FROM botany_samples WHERE batch_id=?), "
                        "    rows_rejected=rows_rejected+? WHERE id=?",
                        (batch_id, outcome["batch_rejections"].get(batch_id, 0), batch_id),
                    )
                self._audit(
                    "botany.import", "botany_import", str(import_id),
                    {"filename": filename, "sha256": digest, "status": status, **{k: v for k, v in outcome["summary"].items() if k != "batches"}},
                    project_id=project_id, actor_id=actor_id,
                )
        except ServiceError:
            raise
        except Exception as exc:  # 意外错误：事务已回滚，不留半成品
            raise ServiceError("import_failed", f"导入中断，已全部回滚：{exc}", 500) from exc

        return {
            "status": status,
            "import_id": import_id,
            "filename": filename,
            "content_sha256": digest,
            "accepted_rows": outcome["accepted"],
            "rejected_rows": outcome["rejected"],
            "duplicate_lines": outcome["duplicates"],
            "batches": outcome["summary"]["batches"],
            "rejections_url": f"/api/projects/{project_id}/botany/imports/{import_id}/rejections",
        }

    def _process_rows(
        self,
        db: sqlite3.Connection,
        project_id: int,
        actor_id: int,
        import_id: int,
        sheet: csvio.ParsedSheet,
        stamp: str,
        *,
        on_row: Callable[[csvio.RawRow], None] | None,
    ) -> dict[str, Any]:
        # 预载项目既有批次/样品/分类，供跨文件续录校验
        existing_batches = {r["batch_code"]: dict(r) for r in db.execute("SELECT * FROM botany_batches WHERE project_id=?", (project_id,))}
        existing_samples = {r["sample_code"]: dict(r) for r in db.execute("SELECT id,sample_code,batch_id,context_id FROM botany_samples WHERE project_id=?", (project_id,))}
        existing_components: dict[tuple[str, str], int] = {
            (r["sample_code"], r["fraction"]): r["id"]
            for r in db.execute(
                "SELECT c.id AS id, s.sample_code AS sample_code, c.fraction AS fraction "
                "FROM botany_components c JOIN botany_samples s ON s.id=c.sample_id WHERE s.project_id=?",
                (project_id,),
            )
        }
        existing_taxa = {r["taxon_code"]: dict(r) for r in db.execute("SELECT * FROM botany_taxa WHERE project_id=?", (project_id,))}

        batches: dict[str, dict[str, Any]] = {}
        contexts: dict[str, str] = {}
        samples: dict[str, dict[str, Any]] = {}
        components: dict[tuple[str, str], int] = {}
        taxa: dict[str, dict[str, Any]] = {k: {"id": v["id"], "is_unknown": v["is_unknown"]} for k, v in existing_taxa.items()}

        accepted = rejected = duplicates = 0
        batch_samples: dict[int, int] = {}
        batch_rejections: dict[int, int] = {}
        batch_seen: set[str] = set()
        rejections: list[tuple[int, str, str, str]] = []

        def reject(row: csvio.RawRow, code: str, message: str, batch_id: int | None = None) -> None:
            nonlocal rejected
            rejected += 1
            rejections.append((row.line, code, message, row.raw_line))
            db.execute(
                "INSERT INTO botany_rejections(import_id,row_number,sheet,raw_line,error_code,error_message,created_at) VALUES(?,?,?,?,?,?,?)",
                (import_id, row.line, row.kind, row.raw_line, code, message, stamp),
            )
            if batch_id is not None:
                batch_rejections[batch_id] = batch_rejections.get(batch_id, 0) + 1

        locked_batch_ids = {bid for bid, in db.execute("SELECT id FROM botany_batches WHERE project_id=? AND status='locked'", (project_id,))}

        def ensure_sample_unlocked(sample_code: str) -> dict[str, Any]:
            sample = samples.get(sample_code) or existing_samples.get(sample_code)
            if sample is not None and sample["batch_id"] in locked_batch_ids:
                raise csvio.RowError("batch_locked", f"样品 {sample_code} 所属批次已确认锁定，不能再追加数据")
            return sample

        for row in sheet.rows:
            if on_row is not None:
                on_row(row)  # 测试用中断钩子；抛出异常会导致整事务回滚
            f = row.fields
            try:
                if row.kind == "batch":
                    code = csvio.require(f.get("batch_code", ""), "batch_code/批次")
                    if code in batch_seen:
                        raise csvio.RowError("duplicate_in_file", f"批次 {code} 在同一文件中重复定义")
                    batch_seen.add(code)
                    if code in existing_batches:
                        # 续录到既有批次；已锁定（确认）批次拒绝追加
                        old = existing_batches[code]
                        if old["status"] == "locked":
                            raise csvio.RowError("batch_locked", f"批次 {code} 已确认锁定，不能再追加数据")
                        batches[code] = {"id": old["id"], "locked": False, "new": False}
                    else:
                        cur = db.execute(
                            "INSERT INTO botany_batches(project_id,batch_code,source_file,status,imported_by,imported_at) VALUES(?,?,?,?,?,?)",
                            (project_id, code, "", "imported", actor_id, stamp),
                        )
                        batches[code] = {"id": cur.lastrowid, "locked": False, "new": True}
                        batch_samples[cur.lastrowid] = 0
                    accepted += 1

                elif row.kind == "sample":
                    bcode = csvio.require(f.get("batch_code", ""), "batch_code/批次")
                    scode = csvio.require(f.get("sample_code", ""), "sample_code/样品编号")
                    batch = batches.get(bcode) or (
                        {"id": existing_batches[bcode]["id"], "locked": existing_batches[bcode]["status"] == "locked", "new": False}
                        if bcode in existing_batches else None
                    )
                    if batch is None:
                        raise csvio.RowError("batch_not_found", f"样品 {scode} 引用的批次 {bcode} 未在本文件或项目中登记")
                    if batch["locked"]:
                        raise csvio.RowError("batch_locked", f"批次 {bcode} 已确认锁定，不能再追加样品")
                    if scode in samples or scode in existing_samples:
                        raise csvio.RowError("duplicate_sample", f"样品编号 {scode} 在项目中已存在（重复样品）")
                    ctype_raw = csvio.require(f.get("context_type", ""), "context_type/遗迹类型")
                    ctype = csvio.CONTEXT_TYPE_ALIASES.get(ctype_raw.casefold())
                    if ctype is None:
                        raise csvio.RowError("bad_context_type", f"遗迹类型无法识别：{ctype_raw!r}（可选：古河道/居址/灰坑/其他）")
                    volume = csvio.parse_positive_float(f.get("volume_liters", ""), "土样体积(升)")  # 零体积在此拒绝
                    mesh = None
                    if f.get("mesh_mm", "").strip():
                        mesh = csvio.parse_float(f["mesh_mm"], "筛网规格(mm)")
                        if mesh <= 0:
                            raise csvio.RowError("bad_mesh", "筛网规格必须为正数")
                    polluted = csvio.parse_bool(f.get("polluted", ""), "现代根系污染标记") if f.get("polluted", "").strip() else 0
                    ccode = f.get("context_code", "").strip() or scode
                    if ccode not in contexts:
                        existing_ctx = db.execute("SELECT id,context_type FROM botany_contexts WHERE project_id=? AND context_code=?", (project_id, ccode)).fetchone()
                        if existing_ctx is not None:
                            if existing_ctx["context_type"] != ctype:
                                raise csvio.RowError("context_type_conflict", f"遗迹 {ccode} 已登记为 {existing_ctx['context_type']}，与本行 {ctype} 冲突")
                            contexts[ccode] = existing_ctx["id"]
                        else:
                            cur_ctx = db.execute(
                                "INSERT INTO botany_contexts(project_id,context_code,context_type,context_name,created_at) VALUES(?,?,?,?,?)",
                                (project_id, ccode, ctype, f.get("context_name", ""), stamp),
                            )
                            contexts[ccode] = cur_ctx.lastrowid
                    fingerprint = hashlib.sha256(stable_json({"s": scode, "v": volume, "m": mesh, "p": polluted}).encode()).hexdigest()
                    cur = db.execute(
                        "INSERT INTO botany_samples(project_id,batch_id,context_id,sample_code,volume_liters,mesh_mm,sieve_notes,polluted,pollution_note,sample_fingerprint,row_number,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (project_id, batch["id"], contexts[ccode], scode, volume, mesh, f.get("sieve_notes", ""), polluted, f.get("pollution_note", ""), fingerprint, row.line, stamp),
                    )
                    samples[scode] = {"id": cur.lastrowid, "batch_id": batch["id"], "volume": volume, "polluted": bool(polluted)}
                    batch_samples[batch["id"]] = batch_samples.get(batch["id"], 0) + 1
                    accepted += 1

                elif row.kind == "fraction":
                    scode = csvio.require(f.get("sample_code", ""), "sample_code/样品编号")
                    frac_raw = csvio.require(f.get("fraction", ""), "fraction/浮选组分")
                    frac = csvio.FRACTION_ALIASES.get(frac_raw.casefold())
                    if frac is None:
                        raise csvio.RowError("bad_fraction", f"浮选组分无法识别：{frac_raw!r}（重/轻）")
                    sample = ensure_sample_unlocked(scode)
                    if sample is None:
                        raise csvio.RowError("sample_not_found", f"组分行引用的样品 {scode} 尚未登记（样品行需位于组分行之前）")
                    key = (scode, frac)
                    comp_id = components.get(key) or existing_components.get(key)
                    if comp_id is not None:
                        raise csvio.RowError("duplicate_fraction", f"样品 {scode} 的{frac}组分已存在")
                    cur = db.execute(
                        "INSERT INTO botany_components(sample_id,fraction,notes,created_at) VALUES(?,?,?,?)",
                        (sample["id"], frac, f.get("fraction_notes", "") or f.get("notes", ""), stamp),
                    )
                    components[key] = cur.lastrowid
                    accepted += 1

                elif row.kind == "identification":
                    scode = csvio.require(f.get("sample_code", ""), "sample_code/样品编号")
                    frac_raw = csvio.require(f.get("fraction", ""), "fraction/浮选组分")
                    frac = csvio.FRACTION_ALIASES.get(frac_raw.casefold())
                    if frac is None:
                        raise csvio.RowError("bad_fraction", f"浮选组分无法识别：{frac_raw!r}（重/轻）")
                    sample = ensure_sample_unlocked(scode)
                    if sample is None:
                        raise csvio.RowError("sample_not_found", f"鉴定行引用的样品 {scode} 尚未登记")
                    key = (scode, frac)
                    comp_id = components.get(key) or existing_components.get(key)
                    if comp_id is None:
                        comp = db.execute(
                            "SELECT c.id FROM botany_components c JOIN botany_samples s ON s.id=c.sample_id WHERE s.sample_code=? AND c.fraction=? AND s.project_id=?",
                            (scode, frac, project_id),
                        ).fetchone()
                        if comp is None:
                            raise csvio.RowError("fraction_not_found", f"鉴定行引用的 {scode}/{frac} 组分尚未登记")
                        comp_id = comp["id"]
                        existing_components[key] = comp_id
                    unknown = csvio.parse_bool(f.get("is_unknown", ""), "未知分类标记") if f.get("is_unknown", "").strip() else 0
                    tcode = f.get("taxon_code", "").strip()
                    if not tcode:
                        if unknown:
                            tcode = f"UNKNOWN:{row.line}"
                        else:
                            raise csvio.RowError("missing_taxon", "鉴定行缺少 taxon_code（分类编号）；若为未知分类请勾选未知并填写未知说明")
                    taxon = taxa.get(tcode)
                    if taxon is None:
                        cur = db.execute(
                            "INSERT INTO botany_taxa(project_id,taxon_code,scientific_name,family,is_unknown,unknown_detail,created_at) VALUES(?,?,?,?,?,?,?)",
                            (project_id, tcode, f.get("scientific_name", ""), f.get("family", ""), unknown, f.get("unknown_detail", ""), stamp),
                        )
                        taxon = {"id": cur.lastrowid, "is_unknown": unknown}
                        taxa[tcode] = taxon
                    conf_raw = f.get("confidence", "").strip() or "high"
                    confidence = csvio.CONFIDENCE_ALIASES.get(conf_raw.casefold())
                    if confidence is None:
                        raise csvio.RowError("bad_confidence", f"鉴定置信度无法识别：{conf_raw!r}（高/中/低/存疑）")
                    censor, count_value, _ = csvio.parse_count(csvio.require(f.get("count", ""), "count/计数"))
                    limit = None
                    if f.get("detection_limit", "").strip():
                        limit = csvio.parse_float(f["detection_limit"], "检测限")
                        if limit < 0:
                            raise csvio.RowError("bad_detection_limit", "检测限不能为负")
                    try:
                        db.execute(
                            "INSERT INTO botany_identifications(component_id,taxon_id,confidence,censor,count_value,detection_limit,is_unknown,notes,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (comp_id, taxon["id"], confidence, censor, count_value, limit, unknown, f.get("notes", ""), stamp),
                        )
                    except sqlite3.IntegrityError:
                        # 同一组分/分类/删失/计数/置信度的重复鉴定行：跳过而非累加
                        duplicates += 1
                        continue
                    accepted += 1

                else:
                    raise csvio.RowError("bad_record_type", f"无法识别的记录类型：{row.kind!r}（应为 batch/sample/fraction/identification）")

            except csvio.RowError as exc:
                # 找到该样品对应的批次（若能解析），归入批次错误计数
                bid = None
                bc = f.get("batch_code", "").strip()
                if bc in batches:
                    bid = batches[bc]["id"]
                reject(row, exc.code, str(exc), bid)
                continue

        batch_summary = []
        for code, info in batches.items():
            batch_summary.append({"batch_code": code, "batch_id": info["id"], "samples": batch_samples.get(info["id"], 0), "rejected_rows": batch_rejections.get(info["id"], 0)})
        return {
            "accepted": accepted,
            "rejected": rejected,
            "duplicates": duplicates,
            "batch_samples": batch_samples,
            "batch_rejections": batch_rejections,
            "summary": {"batches": batch_summary},
            "rejections": rejections,
        }

    # ---- 拒绝清单下载 -------------------------------------------------------

    def rejections_csv(self, project_id: int, user_id: int, import_id: int) -> tuple[str, str]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM botany_imports WHERE id=? AND project_id=?", (import_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("import_not_found", "导入记录不存在", 404)
        rej = self.db.execute(
            "SELECT row_number,error_code,error_message,raw_line FROM botany_rejections WHERE import_id=? ORDER BY row_number,id",
            (import_id,),
        ).fetchall()
        body = csvio.build_rejection_csv([], [(r["row_number"], r["error_code"], r["error_message"], r["raw_line"]) for r in rej])
        return f"rejections-import-{import_id}.csv", body

    # ---- 批次与数据浏览 -----------------------------------------------------

    def list_batches(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute("SELECT * FROM botany_batches WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    def lock_batch(self, project_id: int, user_id: int, batch_id: int) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, EDIT_ROLES)
        row = self.db.execute("SELECT * FROM botany_batches WHERE id=? AND project_id=?", (batch_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("batch_not_found", "批次不存在", 404)
        if row["status"] == "locked":
            return dict(row)
        with transaction(immediate=True) as db:
            db.execute("UPDATE botany_batches SET status='locked',locked_at=? WHERE id=?", (now(), batch_id))
            self._audit("botany.batch.lock", "botany_batch", str(batch_id), {"batch_code": row["batch_code"]}, project_id=project_id, actor_id=user_id)
        return dict(db.execute("SELECT * FROM botany_batches WHERE id=?", (batch_id,)).fetchone())

    def list_imports(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute("SELECT id,filename,status,content_sha256,accepted_rows,rejected_rows,duplicate_lines,created_at FROM botany_imports WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 分类口径（视图） ---------------------------------------------------

    def create_view(self, project_id: int, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, {"owner", "researcher"})
        stamp = now()
        mappings = payload.get("mappings", [])
        with transaction(immediate=True) as db:
            try:
                cur = db.execute(
                    "INSERT INTO botany_taxonomy_views(project_id,view_code,label,created_by,created_at) VALUES(?,?,?,?,?)",
                    (project_id, payload["view_code"], payload.get("label", payload["view_code"]), user_id, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError("view_exists", "分类口径编码已存在", 409) from exc
            view_id = cur.lastrowid
            for m in mappings:
                taxon = db.execute("SELECT id FROM botany_taxa WHERE project_id=? AND taxon_code=?", (project_id, m["taxon_code"])).fetchone()
                if taxon is None:
                    raise ServiceError("taxon_not_found", f"分类编号不存在：{m['taxon_code']}", 422)
                db.execute(
                    "INSERT INTO botany_view_mappings(view_id,taxon_id,mapped_code,mapped_label) VALUES(?,?,?,?)",
                    (view_id, taxon["id"], m["mapped_code"], m.get("mapped_label", m["mapped_code"])),
                )
            self._audit("botany.view.create", "botany_view", str(view_id), payload, project_id=project_id, actor_id=user_id)
        return self.get_view(project_id, user_id, view_id)

    def list_views(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        return [dict(r) for r in self.db.execute("SELECT * FROM botany_taxonomy_views WHERE project_id=? ORDER BY id", (project_id,)).fetchall()]

    def get_view(self, project_id: int, user_id: int, view_id: int) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM botany_taxonomy_views WHERE id=? AND project_id=?", (view_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("view_not_found", "分类口径不存在", 404)
        mappings = self.db.execute("SELECT t.taxon_code, v.mapped_code, v.mapped_label FROM botany_view_mappings v JOIN botany_taxa t ON t.id=v.taxon_id WHERE v.view_id=? ORDER BY v.id", (view_id,)).fetchall()
        return {**dict(row), "mappings": [dict(m) for m in mappings]}

    # ---- 数据装载与统计 -----------------------------------------------------

    def _load_observations(self, project_id: int, *, view_id: int | None, include_polluted: bool, context_types: Iterable[str] | None) -> list[dict[str, Any]]:
        """装载样品及其鉴定计数。

        没有任何鉴定行的样品作为全零样品纳入（出现率口径的分母必须包含它们）。
        """
        sample_sql = (
            "SELECT s.id AS sample_id,s.sample_code,s.volume_liters,s.polluted,c.context_type "
            "FROM botany_samples s JOIN botany_contexts c ON c.id=s.context_id WHERE s.project_id=?"
        )
        params: list[Any] = [project_id]
        if not include_polluted:
            sample_sql += " AND s.polluted=0"
        if context_types:
            wanted = list(context_types)
            sample_sql += f" AND c.context_type IN ({','.join('?' for _ in wanted)})"
            params.extend(wanted)

        samples: dict[int, dict[str, Any]] = {}
        for r in self.db.execute(sample_sql, params).fetchall():
            samples[r["sample_id"]] = {
                "sample_key": r["sample_code"],
                "context_type": r["context_type"],
                "volume_liters": r["volume_liters"],
                "polluted": bool(r["polluted"]),
                "counts": [],
                "taxa": {},
            }
        if not samples:
            return []

        ids = list(samples)
        count_sql = (
            "SELECT s.id AS sample_id,t.taxon_code,t.is_unknown AS taxon_unknown,vm.mapped_code,vm.mapped_label,"
            "       i.censor,i.count_value,i.detection_limit,i.is_unknown AS id_unknown"
            "  FROM botany_samples s"
            "  JOIN botany_components comp ON comp.sample_id=s.id"
            "  JOIN botany_identifications i ON i.component_id=comp.id"
            "  JOIN botany_taxa t ON t.id=i.taxon_id"
            "  LEFT JOIN botany_view_mappings vm ON vm.taxon_id=t.id AND vm.view_id=?"
            f" WHERE s.id IN ({','.join('?' for _ in ids)}) ORDER BY s.id, comp.id, i.id"
        )
        for r in self.db.execute(count_sql, [view_id or 0, *ids]).fetchall():
            sample = samples[r["sample_id"]]
            code = r["mapped_code"] or r["taxon_code"]
            label = r["mapped_label"] or r["taxon_code"]
            count = stats.Count(count=r["count_value"], censor=r["censor"], limit=r["detection_limit"])
            sample["counts"].append(count)
            bucket = sample["taxa"].setdefault(code, {"label": label, "is_unknown": bool(r["taxon_unknown"] or r["id_unknown"]), "counts": []})
            bucket["counts"].append(count)
        return list(samples.values())

    def _build_result(
        self,
        project_id: int,
        *,
        view_id: int | None,
        include_polluted: bool,
        context_types: Iterable[str] | None,
        seed: int,
        iterations: int,
        confidence: float,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        observations = self._load_observations(project_id, view_id=view_id, include_polluted=include_polluted, context_types=context_types)
        samples = [stats.Sample(o["sample_key"], o["context_type"], o["volume_liters"], o["polluted"], tuple(o["counts"])) for o in observations]
        overall = stats.compare_groups(samples, seed=seed, iterations=iterations, confidence=confidence, include_polluted=include_polluted)

        # 按解析后的分类口径逐分类计算（缺失该分类的样品补零，保证出现率分母正确）
        taxon_catalog: dict[str, dict[str, Any]] = {}
        for o in observations:
            for code, bucket in o["taxa"].items():
                taxon_catalog.setdefault(code, {"label": bucket["label"], "is_unknown": bucket["is_unknown"]})
        taxon_results: list[dict[str, Any]] = []
        for code in sorted(taxon_catalog):
            meta = taxon_catalog[code]
            per_taxon_samples = [
                stats.Sample(o["sample_key"], o["context_type"], o["volume_liters"], o["polluted"],
                             tuple(o["taxa"][code]["counts"]) if code in o["taxa"] else (stats.Count(0, "exact"),))
                for o in observations
            ]
            result = stats.compare_groups(per_taxon_samples, seed=seed, iterations=iterations, confidence=confidence, include_polluted=include_polluted)
            groups = {}
            for gname, g in result["groups"].items():
                members = [o for o in observations if o["context_type"] == gname]
                below = sum(1 for o in members for c in o["taxa"].get(code, {"counts": []})["counts"] if c.censor == "below")
                nd = sum(1 for o in members for c in o["taxa"].get(code, {"counts": []})["counts"] if c.censor == "nd")
                groups[gname] = {
                    **{k: v for k, v in g.items() if k not in {"excluded_zero_volume", "excluded_polluted"}},
                    "below_detections": below,
                    "nd_records": nd,
                }
            taxon_results.append({"mapped_code": code, "mapped_label": meta["label"], "is_unknown": meta["is_unknown"], "groups": groups, "contrasts": result["contrasts"]})

        return observations, {
            "overall": overall,
            "taxa": taxon_results,
            "sample_count_total": len(observations),
            "context_types": list(context_types) if context_types else None,
        }

    def run_comparison(
        self,
        project_id: int,
        user_id: int,
        payload: dict[str, Any],
        *,
        persist: bool = True,
        changeset_id: int | None = None,
    ) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, EXPERT_ROLES if persist else READ_ROLES)
        view_id = payload.get("view_id")
        if view_id is not None:
            if self.db.execute("SELECT 1 FROM botany_taxonomy_views WHERE id=? AND project_id=?", (view_id, project_id)).fetchone() is None:
                raise ServiceError("view_not_found", "分类口径不存在", 404)
        iterations = int(payload.get("iterations", 2000))
        confidence = float(payload.get("confidence", 0.95))
        include_polluted = bool(payload.get("include_polluted", False))
        seed = payload.get("seed")
        if seed is None:
            seed = secrets.randbelow(2**31 - 1) + 1
        seed = int(seed)
        context_types = payload.get("context_types")

        observations, result = self._build_result(
            project_id, view_id=view_id, include_polluted=include_polluted,
            context_types=context_types, seed=seed, iterations=iterations, confidence=confidence,
        )
        del observations
        params = {
            "view_id": view_id, "iterations": iterations, "confidence": confidence,
            "include_polluted": include_polluted, "context_types": context_types or None,
        }
        if not persist:
            return {"seed": seed, "algorithm_version": stats.ALGORITHM_VERSION, "params": params, "result": result}

        stamp = now()
        with transaction(immediate=True) as db:
            number = (db.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM botany_stat_versions WHERE project_id=?", (project_id,)).fetchone())[0]
            cur = db.execute(
                "INSERT INTO botany_stat_versions(project_id,version_number,label,view_id,changeset_id,seed,iterations,confidence,include_polluted,algorithm_version,params_json,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, number, payload.get("label", ""), view_id, changeset_id, seed, iterations, confidence, int(include_polluted),
                 stats.ALGORITHM_VERSION, stable_json(params), stable_json(result), user_id, stamp),
            )
            version_id = cur.lastrowid
            self._audit("botany.stat.run", "botany_stat_version", str(version_id), {"version_number": number, "seed": seed, "iterations": iterations, "view_id": view_id}, project_id=project_id, actor_id=user_id)
        return self.get_stat_version(project_id, user_id, version_id)

    def list_stat_versions(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute(
            "SELECT id,version_number,label,view_id,changeset_id,seed,iterations,confidence,include_polluted,algorithm_version,created_by,created_at FROM botany_stat_versions WHERE project_id=? ORDER BY version_number",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stat_version(self, project_id: int, user_id: int, version_id: int) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM botany_stat_versions WHERE id=? AND project_id=?", (version_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("stat_version_not_found", "统计版本不存在", 404)
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json"))
        data["result"] = json.loads(data.pop("result_json"))
        return data

    def recompute(self, project_id: int, user_id: int, version_id: int) -> dict[str, Any]:
        """用历史版本保存的种子/迭代次数/算法版本复算，并比对逐位一致性。"""
        saved = self.get_stat_version(project_id, user_id, version_id)
        if saved["algorithm_version"] != stats.ALGORITHM_VERSION:
            raise ServiceError("algorithm_mismatch", f"当前算法版本 {stats.ALGORITHM_VERSION} 与历史版本 {saved['algorithm_version']} 不一致，无法复算", 409)
        rerun = self.run_comparison(project_id, user_id, {**saved["params"], "seed": saved["seed"]}, persist=False)
        consistent = rerun["result"] == saved["result"]
        return {"version_id": version_id, "consistent": consistent, "saved_seed": saved["seed"], "rerun": rerun}

    # ---- 分类变更集 ---------------------------------------------------------

    def propose_changeset(self, project_id: int, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, EXPERT_ROLES)
        stamp = now()
        items = payload.get("items", [])
        if not items:
            raise ServiceError("empty_changeset", "变更集至少包含一条更名或合并意见", 422)
        seed = int(payload.get("seed") or (secrets.randbelow(2**31 - 1) + 1))
        iterations = int(payload.get("iterations", 2000))
        with transaction(immediate=True) as db:
            cur = db.execute(
                "INSERT INTO botany_taxonomy_changesets(project_id,title,rationale,status,proposed_by,seed,iterations,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, payload["title"], payload.get("rationale", ""), "pending", user_id, seed, iterations, stamp),
            )
            changeset_id = cur.lastrowid
            for item in items:
                action = item.get("action")
                if action not in {"rename", "merge"}:
                    raise ServiceError("bad_change_action", "变更动作只能是 rename 或 merge", 422)
                taxon = db.execute("SELECT id FROM botany_taxa WHERE project_id=? AND taxon_code=?", (project_id, item["taxon_code"])).fetchone()
                if taxon is None:
                    raise ServiceError("taxon_not_found", f"分类编号不存在：{item['taxon_code']}", 422)
                target_id = None
                if action == "merge":
                    target = db.execute("SELECT id FROM botany_taxa WHERE project_id=? AND taxon_code=?", (project_id, item["target_taxon_code"])).fetchone()
                    if target is None:
                        raise ServiceError("target_not_found", f"合并目标不存在：{item['target_taxon_code']}", 422)
                    if target["id"] == taxon["id"]:
                        raise ServiceError("bad_merge", "分类不能合并到自身", 422)
                    target_id = target["id"]
                else:
                    if not item.get("new_code"):
                        raise ServiceError("missing_new_code", "更名需要 new_code", 422)
                db.execute(
                    "INSERT INTO botany_taxonomy_changes(changeset_id,taxon_id,action,new_name,new_code,target_taxon_id,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (changeset_id, taxon["id"], action, item.get("new_name") or "", item.get("new_code") or "", target_id, item.get("note") or "", stamp),
                )
            self._audit("botany.changeset.propose", "botany_changeset", str(changeset_id), {"title": payload["title"], "items": len(items)}, project_id=project_id, actor_id=user_id)
        return self.get_changeset(project_id, user_id, changeset_id)

    def list_changesets(self, project_id: int, user_id: int, status_filter: str | None = None) -> list[dict[str, Any]]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        sql = "SELECT * FROM botany_taxonomy_changesets WHERE project_id=?"
        params: list[Any] = []
        if status_filter:
            sql += " AND status=?"
            params.append(status_filter)
        sql += " ORDER BY id"
        params.insert(0, project_id)
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def get_changeset(self, project_id: int, user_id: int, changeset_id: int) -> dict[str, Any]:
        self.foundation.require_role(project_id, user_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM botany_taxonomy_changesets WHERE id=? AND project_id=?", (changeset_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("changeset_not_found", "变更集不存在", 404)
        items = self.db.execute(
            "SELECT ch.id,ch.action,ch.new_name,ch.new_code,ch.note,t.taxon_code,tt.taxon_code AS target_taxon_code "
            "FROM botany_taxonomy_changes ch JOIN botany_taxa t ON t.id=ch.taxon_id "
            "LEFT JOIN botany_taxa tt ON tt.id=ch.target_taxon_id WHERE ch.changeset_id=? ORDER BY ch.id",
            (changeset_id,),
        ).fetchall()
        return {**dict(row), "items": [dict(i) for i in items]}

    def _snapshot_view(self, db: sqlite3.Connection, project_id: int, label: str, user_id: int, changes: list[sqlite3.Row], base_view_id: int | None, stamp: str) -> int:
        """把当前原始分类 + 既有口径映射 + 本变更集合成为一个新视图（不改写原始鉴定）。"""
        cur = db.execute(
            "INSERT INTO botany_taxonomy_views(project_id,view_code,label,created_by,created_at) VALUES(?,?,?,?,?)",
            (project_id, f"AUTO-CS-{secrets.token_hex(4)}", label, user_id, stamp),
        )
        view_id = cur.lastrowid

        # 解析合并链：rename A->A'；merge A->B（B 若也在变更集中被改名/合并，顺链走到终点）
        by_taxon: dict[int, dict[str, Any]] = {}
        for ch in changes:
            by_taxon[ch["taxon_id"]] = {"action": ch["action"], "new_code": ch["new_code"], "new_name": ch["new_name"], "target": ch["target_taxon_id"]}

        def resolve(taxon_id: int, seen: frozenset[int] = frozenset()) -> tuple[str, str]:
            if taxon_id in by_taxon:
                change = by_taxon[taxon_id]
                if taxon_id in seen:
                    raise ServiceError("merge_cycle", "合并意见中存在循环，无法批准", 422)
                if change["action"] == "rename":
                    return change["new_code"], change["new_name"] or change["new_code"]
                return resolve(change["target"], seen | {taxon_id})
            if base_view_id:
                mapped = db.execute("SELECT mapped_code,mapped_label FROM botany_view_mappings WHERE view_id=? AND taxon_id=?", (base_view_id, taxon_id)).fetchone()
                if mapped:
                    return mapped["mapped_code"], mapped["mapped_label"]
            t = db.execute("SELECT taxon_code,scientific_name FROM botany_taxa WHERE id=?", (taxon_id,)).fetchone()
            return t["taxon_code"], t["scientific_name"] or t["taxon_code"]

        taxa = db.execute("SELECT id FROM botany_taxa WHERE project_id=?", (project_id,)).fetchall()
        for t in taxa:
            mapped_code, mapped_label = resolve(t["id"])
            db.execute("INSERT INTO botany_view_mappings(view_id,taxon_id,mapped_code,mapped_label) VALUES(?,?,?,?)", (view_id, t["id"], mapped_code, mapped_label))
        return view_id

    def review_changeset(self, project_id: int, user_id: int, changeset_id: int, decision: str, note: str = "") -> dict[str, Any]:
        """批准（approve）或驳回（reject）变更集。批准会快照新口径并生成新统计版本。"""
        self.foundation.require_role(project_id, user_id, APPROVE_ROLES)
        if decision not in {"approved", "rejected"}:
            raise ServiceError("bad_decision", "决定只能是 approved 或 rejected", 422)
        stamp = now()
        with transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM botany_taxonomy_changesets WHERE id=? AND project_id=?", (changeset_id, project_id)).fetchone()
            if row is None:
                raise ServiceError("changeset_not_found", "变更集不存在", 404)
            if row["status"] != "pending":
                raise ServiceError("changeset_decided", f"变更集已{row['status']}，不能重复审定", 409)

            if decision == "rejected":
                db.execute("UPDATE botany_taxonomy_changesets SET status='rejected',reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?", (user_id, note, stamp, changeset_id))
                self._audit("botany.changeset.reject", "botany_changeset", str(changeset_id), {"note": note}, project_id=project_id, actor_id=user_id)
                return self.get_changeset(project_id, user_id, changeset_id)

            changes = db.execute("SELECT * FROM botany_taxonomy_changes WHERE changeset_id=?", (changeset_id,)).fetchall()
            latest_view = db.execute("SELECT id FROM botany_taxonomy_views WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
            base_view_id = latest_view["id"] if latest_view else None
            view_id = self._snapshot_view(db, project_id, f"变更集 #{changeset_id}：{row['title']}", user_id, changes, base_view_id, stamp)

            db.execute("UPDATE botany_taxonomy_changesets SET status='approved',reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?", (user_id, note, stamp, changeset_id))

            # 在同一事务内基于新口径生成统计版本（原始鉴定保持不变，仅视图映射改变）
            observations, result = self._build_result(
                project_id, view_id=view_id, include_polluted=False, context_types=None,
                seed=row["seed"], iterations=row["iterations"], confidence=0.95,
            )
            number = (db.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM botany_stat_versions WHERE project_id=?", (project_id,)).fetchone())[0]
            params = {"view_id": view_id, "iterations": row["iterations"], "confidence": 0.95, "include_polluted": False, "context_types": None, "origin": f"changeset-{changeset_id}"}
            cur = db.execute(
                "INSERT INTO botany_stat_versions(project_id,version_number,label,view_id,changeset_id,seed,iterations,confidence,include_polluted,algorithm_version,params_json,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, number, f"变更集 #{changeset_id} 批准版本", view_id, changeset_id, row["seed"], row["iterations"], 0.95, 0,
                 stats.ALGORITHM_VERSION, stable_json(params), stable_json(result), user_id, stamp),
            )
            stat_version_id = cur.lastrowid
            db.execute("UPDATE botany_taxonomy_changesets SET stat_version_id=? WHERE id=?", (stat_version_id, changeset_id))
            self._audit("botany.changeset.approve", "botany_changeset", str(changeset_id), {"view_id": view_id, "stat_version_id": stat_version_id}, project_id=project_id, actor_id=user_id)
        return self.get_changeset(project_id, user_id, changeset_id)

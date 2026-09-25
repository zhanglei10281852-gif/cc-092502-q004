"""植物遗存分析模块的 HTTP 接口，全部按项目角色保护。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app.paleo.csvio import IMPORT_KINDS
from app.paleo.service import (
    CHANGESET_PROPOSE_ROLES,
    CHANGESET_REVIEW_ROLES,
    CONFIRM_ROLES,
    IMPORT_ROLES,
    READ_ROLES,
    STATS_RUN_ROLES,
    PaleoService,
)
from app.service import ResearchService

router = APIRouter(prefix="/api/projects/{project_id}/paleo", tags=["paleo"])


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


def _check(project_id: int, user, roles: set[str]) -> None:
    ResearchService().require_role(project_id, user["id"], roles)


class StatsRunCreate(BaseModel):
    rank: str = "species"
    seed: int | None = None
    iterations: int | None = None
    include_contaminated: bool = False
    min_confidence: str | None = None


class ChangesetOp(BaseModel):
    op: str = Field(..., pattern="^(rename|merge)$")
    from_: list[str] | str = Field(..., alias="from")
    to: str = Field(..., min_length=1)

    model_config = {"populate_by_name": True}


class ChangesetCreate(BaseModel):
    note: str = ""
    ops: list[ChangesetOp]
    stats_params: dict = Field(default_factory=dict)


class ReviewAction(BaseModel):
    review_note: str = ""


@router.post("/imports/{kind}", status_code=201)
def import_csv(project_id: int, kind: str, file: UploadFile = File(...), user=Depends(current_user)):
    _check(project_id, user, IMPORT_ROLES)
    if kind not in IMPORT_KINDS:
        from app.service import ServiceError
        raise ServiceError("bad_kind", f"导入类别必须是 {list(IMPORT_KINDS)} 之一", 422)
    content = file.file.read()
    return PaleoService().import_csv(project_id, kind, file.filename or "", content, user["id"])


@router.get("/imports")
def list_imports(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": PaleoService().list_imports(project_id)}


@router.get("/imports/{import_id}")
def get_import(project_id: int, import_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return PaleoService().get_import(project_id, import_id)


@router.get("/imports/{import_id}/rejects")
def download_rejects(project_id: int, import_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    csv_text = PaleoService().import_rejects_csv(project_id, import_id)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="rejects-import-{import_id}.csv"'},
    )


@router.get("/samples")
def list_samples(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": PaleoService().list_samples(project_id)}


@router.get("/batches")
def list_batches(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": PaleoService().list_batches(project_id)}


@router.post("/batches/{batch_code}/confirm")
def confirm_batch(project_id: int, batch_code: str, user=Depends(current_user)):
    _check(project_id, user, CONFIRM_ROLES)
    return PaleoService().confirm_batch(project_id, batch_code, user["id"])


@router.post("/stats/runs", status_code=201)
def create_stats_run(project_id: int, payload: StatsRunCreate, user=Depends(current_user)):
    _check(project_id, user, STATS_RUN_ROLES)
    params = {key: value for key, value in payload.model_dump().items() if value is not None}
    return PaleoService().create_stats_run(project_id, params, user["id"])


@router.get("/stats/runs")
def list_stats_runs(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": PaleoService().list_stats_runs(project_id)}


@router.get("/stats/runs/{run_id}")
def get_stats_run(project_id: int, run_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return PaleoService().get_stats_run(project_id, run_id)


@router.post("/changesets", status_code=201)
def propose_changeset(project_id: int, payload: ChangesetCreate, user=Depends(current_user)):
    _check(project_id, user, CHANGESET_PROPOSE_ROLES)
    ops = [{"op": op.op, "from": op.from_, "to": op.to} for op in payload.ops]
    body = {"note": payload.note, "ops": ops, "stats_params": payload.stats_params}
    return PaleoService().propose_changeset(project_id, body, user["id"])


@router.get("/changesets")
def list_changesets(project_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return {"data": PaleoService().list_changesets(project_id)}


@router.get("/changesets/{changeset_id}")
def get_changeset(project_id: int, changeset_id: int, user=Depends(current_user)):
    _check(project_id, user, READ_ROLES)
    return PaleoService().get_changeset(project_id, changeset_id)


@router.post("/changesets/{changeset_id}/approve")
def approve_changeset(project_id: int, changeset_id: int, payload: ReviewAction | None = None,
                      user=Depends(current_user)):
    _check(project_id, user, CHANGESET_REVIEW_ROLES)
    note = payload.review_note if payload else ""
    return PaleoService().approve_changeset(project_id, changeset_id, user["id"], note)


@router.post("/changesets/{changeset_id}/reject")
def reject_changeset(project_id: int, changeset_id: int, payload: ReviewAction | None = None,
                     user=Depends(current_user)):
    _check(project_id, user, CHANGESET_REVIEW_ROLES)
    note = payload.review_note if payload else ""
    return PaleoService().reject_changeset(project_id, changeset_id, user["id"], note)

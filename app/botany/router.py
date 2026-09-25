"""植物考古 HTTP 接口，全部挂在项目下并受项目角色保护。

CSV 上传使用 application/octet-stream（或 text/csv）原始请求体，文件名通过
X-Filename 头或 ?filename= 查询参数给出；离线大批量导入走 `python -m app.cli
botany-import`，不经 HTTP。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from app.botany.schemas import ChangesetCreate, ComparisonRequest, ReviewRequest, ViewCreate
from app.botany.service import BotanyService
from app.service import ResearchService, ServiceError

router = APIRouter(prefix="/api/projects/{project_id}/botany", tags=["botany"])


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise ServiceError("unauthorized", "缺少 Bearer 会话", 401)
    return ResearchService().authenticate(authorization[7:])


@router.post("/imports", status_code=201)
async def create_import(
    project_id: int,
    request: Request,
    filename: str = Query(default="", max_length=240),
    x_filename: str = Header(default="", alias="X-Filename"),
    auth=Depends(current_user),
):
    content = await request.body()
    if not content:
        raise ServiceError("empty_upload", "上传内容为空", 400)
    name = filename or x_filename or "upload.csv"
    return BotanyService().import_csv(project_id, auth["id"], name, content)


@router.get("/imports")
def get_imports(project_id: int, auth=Depends(current_user)):
    return {"data": BotanyService().list_imports(project_id, auth["id"])}


@router.get("/imports/{import_id}/rejections")
def get_rejections(project_id: int, import_id: int, auth=Depends(current_user)):
    service = BotanyService()
    download_name, body = service.rejections_csv(project_id, auth["id"], import_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@router.get("/batches")
def get_batches(project_id: int, auth=Depends(current_user)):
    return {"data": BotanyService().list_batches(project_id, auth["id"])}


@router.post("/batches/{batch_id}/lock")
def lock_batch(project_id: int, batch_id: int, auth=Depends(current_user)):
    return BotanyService().lock_batch(project_id, auth["id"], batch_id)


@router.post("/views", status_code=201)
def create_view(project_id: int, payload: ViewCreate, auth=Depends(current_user)):
    return BotanyService().create_view(project_id, auth["id"], payload.model_dump())


@router.get("/views")
def get_views(project_id: int, auth=Depends(current_user)):
    return {"data": BotanyService().list_views(project_id, auth["id"])}


@router.get("/views/{view_id}")
def get_view(project_id: int, view_id: int, auth=Depends(current_user)):
    return BotanyService().get_view(project_id, auth["id"], view_id)


@router.post("/comparisons")
def run_comparison(project_id: int, payload: ComparisonRequest, persist: bool = Query(default=True), auth=Depends(current_user)):
    result = BotanyService().run_comparison(project_id, auth["id"], payload.model_dump(), persist=persist)
    return JSONResponse(content=result, status_code=201 if persist else 200)


@router.get("/stat-versions")
def get_stat_versions(project_id: int, auth=Depends(current_user)):
    return {"data": BotanyService().list_stat_versions(project_id, auth["id"])}


@router.get("/stat-versions/{version_id}")
def get_stat_version(project_id: int, version_id: int, auth=Depends(current_user)):
    return BotanyService().get_stat_version(project_id, auth["id"], version_id)


@router.post("/stat-versions/{version_id}/recompute")
def recompute_stat_version(project_id: int, version_id: int, auth=Depends(current_user)):
    return BotanyService().recompute(project_id, auth["id"], version_id)


@router.post("/changesets", status_code=201)
def propose_changeset(project_id: int, payload: ChangesetCreate, auth=Depends(current_user)):
    return BotanyService().propose_changeset(project_id, auth["id"], payload.model_dump())


@router.get("/changesets")
def get_changesets(project_id: int, status: str | None = Query(default=None), auth=Depends(current_user)):
    return {"data": BotanyService().list_changesets(project_id, auth["id"], status)}


@router.get("/changesets/{changeset_id}")
def get_changeset(project_id: int, changeset_id: int, auth=Depends(current_user)):
    return BotanyService().get_changeset(project_id, auth["id"], changeset_id)


@router.post("/changesets/{changeset_id}/review")
def review_changeset(project_id: int, changeset_id: int, payload: ReviewRequest, auth=Depends(current_user)):
    return BotanyService().review_changeset(project_id, auth["id"], changeset_id, payload.decision, payload.note)

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import connection, init_db


def _paleo_import(args: argparse.Namespace) -> int:
    """离线批量导入：不经过 HTTP 服务，直接写入数据库。"""
    from app.paleo.csvio import IMPORT_KINDS
    from app.paleo.service import IMPORT_ROLES, PaleoService
    from app.service import ServiceError

    init_db()
    db = connection()
    project = db.execute("SELECT * FROM projects WHERE code=?", (args.project.upper(),)).fetchone()
    if project is None:
        print(json.dumps({"error": {"code": "project_not_found", "message": f"项目不存在: {args.project}"}}, ensure_ascii=False))
        return 1
    user = db.execute("SELECT * FROM users WHERE username=?", (args.user,)).fetchone()
    if user is None:
        print(json.dumps({"error": {"code": "user_not_found", "message": f"用户不存在: {args.user}"}}, ensure_ascii=False))
        return 1
    if args.kind not in IMPORT_KINDS:
        print(json.dumps({"error": {"code": "bad_kind", "message": f"导入类别必须是 {list(IMPORT_KINDS)} 之一"}}, ensure_ascii=False))
        return 1
    path = Path(args.file)
    if not path.is_file():
        print(json.dumps({"error": {"code": "file_not_found", "message": f"文件不存在: {args.file}"}}, ensure_ascii=False))
        return 1
    service = PaleoService(db)
    try:
        service.require(project["id"], user["id"], IMPORT_ROLES)
        result = service.import_csv(project["id"], args.kind, path.name, path.read_bytes(), user["id"])
    except ServiceError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "check-db", "smoke", "paleo-import"])
    parser.add_argument("--project", help="项目编码（paleo-import）")
    parser.add_argument("--kind", help="导入类别: samples/batches/fractions/identifications")
    parser.add_argument("--file", help="CSV 文件路径（paleo-import）")
    parser.add_argument("--user", help="执行导入的用户名（paleo-import）")
    args = parser.parse_args()
    if args.command == "init-db":
        init_db()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    if args.command == "paleo-import":
        missing = [name for name in ("project", "kind", "file", "user") if not getattr(args, name)]
        if missing:
            print(json.dumps({"error": {"code": "missing_args", "message": f"缺少参数: {', '.join('--' + m for m in missing)}"}}, ensure_ascii=False))
            return 1
        return _paleo_import(args)
    from app.main import app
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

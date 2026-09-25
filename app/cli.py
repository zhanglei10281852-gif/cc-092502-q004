from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="考古研究服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db")
    sub.add_parser("check-db")
    sub.add_parser("smoke")

    imp = sub.add_parser("botany-import", help="离线批量导入植物考古 CSV")
    imp.add_argument("--project", required=True, help="项目编码（code）")
    imp.add_argument("--as", dest="actor", required=True, help="以哪个已存在用户名的身份导入（需具备项目写角色）")
    imp.add_argument("--file", required=True, help="CSV 文件路径")
    imp.add_argument("--rejections-out", default="", help="拒绝清单 CSV 输出路径（默认与导入文件同目录）")

    args = parser.parse_args()

    if args.command == "init-db":
        from app.database import init_db
        from app.botany.service import BotanyService
        init_db()
        BotanyService().init_schema()
        print(_json({"status": "initialized"}))
        return 0
    if args.command == "check-db":
        from app.database import connection, init_db
        from app.botany.service import BotanyService
        init_db()
        BotanyService().init_schema()
        db = connection()
        print(_json({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}))
        return 0
    if args.command == "smoke":
        from app.main import app
        with TestClient(app) as client:
            root = client.get("/")
            health = client.get("/api/system/health")
            print(_json({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}))
        return 0
    if args.command == "botany-import":
        return _botany_import(args)
    return 1


def _botany_import(args: argparse.Namespace) -> int:
    from app.database import close_connection, connection, init_db
    from app.botany.service import BotanyService
    from app.service import ServiceError

    init_db()
    BotanyService().init_schema()
    db = connection()
    project = db.execute("SELECT id,code FROM projects WHERE code=?", (args.project.upper(),)).fetchone()
    if project is None:
        print(_json({"error": "project_not_found", "message": f"项目编码不存在：{args.project}"}), file=sys.stderr)
        close_connection()
        return 2
    user = db.execute("SELECT id,username,status FROM users WHERE username=?", (args.actor,)).fetchone()
    if user is None:
        print(_json({"error": "user_not_found", "message": f"用户不存在：{args.actor}"}), file=sys.stderr)
        close_connection()
        return 2

    path = Path(args.file)
    if not path.is_file():
        print(_json({"error": "file_not_found", "message": str(path)}), file=sys.stderr)
        close_connection()
        return 2
    content = path.read_bytes()
    service = BotanyService(db)
    try:
        result = service.import_csv(project["id"], user["id"], path.name, content)
    except ServiceError as exc:
        print(_json({"error": exc.code, "message": exc.message}), file=sys.stderr)
        close_connection()
        return 3

    out_path = Path(args.rejections_out) if args.rejections_out else path.with_suffix(".rejections.csv")
    if result.get("rejected_rows"):
        _, body = service.rejections_csv(project["id"], user["id"], result["import_id"])
        out_path.write_bytes(body.encode("utf-8"))
        result["rejections_file"] = str(out_path)
    print(_json(result))
    close_connection()
    return 0 if result["status"] != "rejected" else 4


if __name__ == "__main__":
    raise SystemExit(main())

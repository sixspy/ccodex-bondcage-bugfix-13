"""审计只读面：全量轨迹 + 快照过期偏航登记。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def list_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/deviations")
def list_deviations(authorization: AuthHeader = None, limit: int = 100):
    """偏航登记：旧快照出金被零增拒写的「快照过期偏航」条目。"""
    require_user(authorization)
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM audit_trail WHERE action='快照过期偏航'
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return {"items": [dict(r) for r in rows]}

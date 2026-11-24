"""入笼冻结：可用保证金划入笼内，写笼代；笼面受益人改口也走这里。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock, try_acquire
from models import CageFreezeIn

router = APIRouter(tags=["cages"])

FATE_OPEN = "已冻结未入审：受益人可改"
FATE_LOCKED = "入审锁册：受益人当日不得改口"


class BeneficiaryChangeIn(BaseModel):
    beneficiary: str = Field(min_length=2, max_length=128)


def _beneficiary_fate(status: str) -> str:
    """快照命运说明：入审锁册后当日不得改口。"""
    return FATE_OPEN if status == "已冻结" else FATE_LOCKED


def _next_cage_code(conn, party_key: str) -> str:
    n = conn.execute(
        "SELECT COUNT(*) c FROM cages WHERE party_key=?", (party_key,)
    ).fetchone()["c"]
    return f"CG-{party_key}-{n + 1:03d}"


@router.get("/api/cages")
def list_cages(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM cages WHERE status=? ORDER BY id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM cages ORDER BY id DESC").fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/cages/{cage_id}")
def get_cage(cage_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        cage = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
        if not cage:
            raise HTTPException(404, "笼不存在")
        atts = conn.execute(
            "SELECT * FROM attestations WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        rels = conn.execute(
            "SELECT * FROM releases WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        fors = conn.execute(
            "SELECT * FROM forfeits WHERE cage_id=? ORDER BY id DESC",
            (cage_id,),
        ).fetchall()
        return {
            "cage": dict(cage),
            "beneficiary_fate": _beneficiary_fate(cage["status"]),
            "attestations": [dict(a) for a in atts],
            "releases": [dict(r) for r in rels],
            "forfeits": [dict(f) for f in fors],
        }


@router.post("/api/cages/{cage_id}/beneficiary")
def change_beneficiary(
    cage_id: int, body: BeneficiaryChangeIn, authorization: AuthHeader = None
):
    """柜面改受益人全称：与同笼出金抢同一把笼锁，后到争用只记痕；
    快照命运：入审锁册后当日不得改口。"""
    user = require_writer(authorization)
    lock_key = f"cage:{cage_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 争用痕落库，业务零写
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            old = cage["beneficiary"]
            new = body.beneficiary.strip()
            if cage["status"] != "已冻结":
                # 快照命运：入审锁册后当日不得改口，改口企图落审计
                write_audit(
                    conn,
                    user,
                    "cage_beneficiary_fate",
                    cage["cage_code"],
                    {
                        "old": old,
                        "wanted": new,
                        "status": cage["status"],
                        "decision": "拒：入审锁册当日不得改口",
                    },
                )
                conn.commit()
                raise HTTPException(409, "入审锁册：受益人当日不得改口")
            conn.execute(
                "UPDATE cages SET beneficiary=? WHERE id=?", (new, cage_id)
            )
            write_audit(
                conn,
                user,
                "cage_beneficiary_change",
                cage["cage_code"],
                {
                    "old": old,
                    "new": new,
                    "generation": int(cage["generation"]),
                    "fate": "已冻结未入审可改；入审锁册后当日不得改口",
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM cages WHERE id=?", (cage_id,)
            ).fetchone()
            out = dict(row)
            out["beneficiary_fate"] = _beneficiary_fate(out["status"])
            return out
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/cages/freeze")
def freeze_cage(body: CageFreezeIn, authorization: AuthHeader = None):
    """从账户可用余额划入新笼并冻结。"""
    user = require_writer(authorization)
    lock_key = f"bond:{body.party_key}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            acct = conn.execute(
                "SELECT * FROM bond_accounts WHERE party_key=?",
                (body.party_key,),
            ).fetchone()
            if not acct:
                raise HTTPException(404, "保证金户不存在")
            if acct["status"] != "正常":
                raise HTTPException(409, "账户状态禁止入笼")
            amount = float(body.amount)
            free = float(acct["balance"]) - float(acct["frozen"])
            if amount > free + 1e-9:
                raise HTTPException(409, "可用保证金不足，无法入笼")
            code = body.cage_code or _next_cage_code(conn, body.party_key)
            dup = conn.execute(
                "SELECT id FROM cages WHERE cage_code=?", (code,)
            ).fetchone()
            if dup:
                raise HTTPException(409, "笼编码已存在")
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO cages(cage_code,party_key,contract_no,amount,available,
                   generation,status,beneficiary,created_at,frozen_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.party_key,
                    body.contract_no,
                    amount,
                    amount,
                    1,
                    "已冻结",
                    body.beneficiary,
                    now,
                    now,
                ),
            )
            cage_id = cur.lastrowid
            conn.execute(
                "UPDATE bond_accounts SET frozen = frozen + ? WHERE party_key=?",
                (amount, body.party_key),
            )
            write_audit(
                conn,
                user,
                "cage_freeze",
                code,
                {
                    "cage_id": cage_id,
                    "amount": amount,
                    "contract_no": body.contract_no,
                    "beneficiary": body.beneficiary,
                    "generation": 1,
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM cages WHERE id=?", (cage_id,)).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()

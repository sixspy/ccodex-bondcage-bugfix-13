"""解笼拨付：核验通过后把笼内可用额拨付给受益人。

甲路径口径：写拨付前必须重读笼面最新受益人快照；请求携带的旧快照
对不上则记「快照过期偏航」，正式零增拒写并回滚解笼草稿。
受益快照只走笼面名册一侧，不与请求缓存名混用。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import ReleaseIn

router = APIRouter(tags=["release"])


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, c.cage_code, c.contract_no, c.beneficiary AS cage_beneficiary
               FROM releases r
               JOIN cages c ON c.id=r.cage_id
               ORDER BY r.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def do_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"cage:{body.cage_id}"
    with connect() as conn:
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 争用痕落库，业务零写
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")
            if cage["status"] != "核验通过":
                raise HTTPException(409, "仅核验通过的笼可解笼拨付")
            # 核验结论必须对齐当前笼代
            att = conn.execute(
                """SELECT * FROM attestations
                   WHERE cage_id=? AND attest_gen=? AND outcome='通过'
                   ORDER BY id DESC LIMIT 1""",
                (body.cage_id, int(cage["generation"])),
            ).fetchone()
            if not att:
                raise HTTPException(409, "缺少与笼代对齐的通过核验")
            avail = float(cage["available"])
            amount = float(body.amount) if body.amount is not None else avail
            if amount <= 0:
                raise HTTPException(400, "拨付金额须大于 0")
            if amount > avail + 1e-9:
                raise HTTPException(409, "拨付金额超过笼可用额")
            gen = int(cage["generation"])
            # 甲路径：写拨付前重读笼面最新受益人快照
            fresh = conn.execute(
                "SELECT beneficiary FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            current_beneficiary = fresh["beneficiary"] if fresh else ""
            if body.payee and body.payee != current_beneficiary:
                # 旧快照作废：正式零增拒写并回滚解笼草稿，记快照过期偏航
                write_audit(
                    conn,
                    user,
                    "快照过期偏航",
                    cage["cage_code"],
                    {
                        "stale_payee": body.payee,
                        "current_beneficiary": current_beneficiary,
                        "amount": amount,
                        "cage_generation": gen,
                        "decision": "零增拒写并回滚解笼草稿",
                    },
                )
                conn.commit()  # 只落偏航痕，拨付册零增、笼面不动
                raise HTTPException(
                    409,
                    "快照过期偏航：收款方快照与笼面最新受益人不符，"
                    "已零增拒写并回滚解笼草稿",
                )
            # 受益快照只走笼面名册一侧，不与请求缓存名混用
            payee = current_beneficiary
            now = now_iso()
            ref = f"REL-{cage['cage_code']}-g{gen}-{int(now.replace(':', '').replace('-', '').replace('T', '')[-8:])}"
            cur = conn.execute(
                """INSERT INTO releases(cage_id,amount,payee,cage_generation,status,release_ref,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (body.cage_id, amount, payee, gen, "已拨付", ref, now),
            )
            new_avail = round(avail - amount, 6)
            new_status = "已解笼" if new_avail <= 1e-9 else "部分解笼"
            conn.execute(
                "UPDATE cages SET available=?, status=? WHERE id=?",
                (max(new_avail, 0.0), new_status, body.cage_id),
            )
            # 账户：冻结减少，余额减少（资金离开系统）
            conn.execute(
                """UPDATE bond_accounts
                   SET frozen = frozen - ?, balance = balance - ?
                   WHERE party_key=?""",
                (amount, amount, cage["party_key"]),
            )
            write_audit(
                conn,
                user,
                "cage_release",
                cage["cage_code"],
                {
                    "release_id": cur.lastrowid,
                    "amount": amount,
                    "payee": payee,
                    "cage_generation": gen,
                    "release_ref": ref,
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM releases WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()

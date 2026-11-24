"""文件库复现/验收：改受益人后仍打出旧名拨付（过期快照事故）。

用法（仓库根目录）：
    .venv/bin/python scripts/repro_stale_snapshot.py

口径：
  * 数据不迁出本仓卷 —— 在 data/repro_stale_snapshot.db 上跑，夹具来自 fixtures/。
  * 事故态（未打补丁）：旧名凭证落册 + 笼面新名 + 对账假绿 → 打印证据，退出码 1。
  * 修复态（已打补丁）：旧快照出金被「快照过期偏航」零增拒写，重读后凭证落新名，
    对账名册一致 → 打印验收通过，退出码 0。
  * 其它意外状态 → 退出码 2。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

REPRO_DB = ROOT / "data" / "repro_stale_snapshot.db"

OLD_NAME = "市交投项目办"
NEW_NAME = "市交通投资运营集团有限公司"
CAGE_CODE = "CG-P-EAST-001"
OUT_AMOUNT = 50000


def main() -> int:
    import db  # noqa: PLC0415

    if REPRO_DB.exists():
        REPRO_DB.unlink()
    db.DB_PATH = REPRO_DB  # 文件库夹具：独立事故库，不动 data/app.db

    from db import init_db  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415

    from app import app  # noqa: PLC0415

    init_db()  # 从 fixtures/*.json 落种子

    evidence: list[str] = []
    with TestClient(app) as c:
        tok = c.post("/api/login", json={"username": "admin", "password": "123456"})
        assert tok.status_code == 200, "浏览器登录照旧：admin/123456 必须能登录"
        h = {"Authorization": "Bearer " + tok.json()["token"]}

        cage = next(
            x for x in c.get("/api/cages", headers=h).json()["items"]
            if x["cage_code"] == CAGE_CODE
        )
        assert cage["beneficiary"] == OLD_NAME and cage["status"] == "已冻结"
        evidence.append(f"[种子] 笼 {CAGE_CODE} 受益人={OLD_NAME} 状态=已冻结")

        # —— 柜员在笼面改受益人全称 ——
        r = c.post(
            f"/api/cages/{cage['id']}/beneficiary",
            headers=h,
            json={"beneficiary": NEW_NAME},
        )
        if r.status_code in (404, 405):
            # 基线没有改受益接口：以文件库级改口模拟柜面改名
            with sqlite3.connect(REPRO_DB) as conn:
                conn.execute(
                    "UPDATE cages SET beneficiary=? WHERE id=?",
                    (NEW_NAME, cage["id"]),
                )
                conn.commit()
            evidence.append("[改口] 基线无改受益接口（404），已按文件库级改口落新名")
        elif r.status_code == 200:
            evidence.append("[改口] 走改受益接口落新名（已冻结未入审，允许）")
        else:
            evidence.append(f"[改口] 意外状态 {r.status_code}: {r.text}")
            print("\n".join(evidence))
            return 2

        face = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
        assert face["beneficiary"] == NEW_NAME
        evidence.append(f"[笼面] 最新受益人={NEW_NAME}")

        # —— 履约核验通过 ——
        att = c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage["id"], "outcome": "通过", "evidence": "验收合格"},
        )
        assert att.status_code == 200
        evidence.append("[核验] 履约核验通过")

        # —— 出金：请求仍带创建时缓存的旧名快照 ——
        rel = c.post(
            "/api/releases",
            headers=h,
            json={"cage_id": cage["id"], "amount": OUT_AMOUNT, "payee": OLD_NAME},
        )
        evidence.append(
            f"[出金] 按旧名快照拨付 {OUT_AMOUNT} → HTTP {rel.status_code}"
        )

        releases = [
            x for x in c.get("/api/releases", headers=h).json()["items"]
            if x["cage_id"] == cage["id"]
        ]
        recon = c.get(f"/api/reconcile/{CAGE_CODE}", headers=h).json()
        face_after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]

        if rel.status_code == 200:
            voucher = releases[0]
            evidence.append(
                f"[凭证] {voucher['release_ref']} 收款方={voucher['payee']}（笼面={face_after['beneficiary']}）"
            )
            evidence.append(
                f"[对账] balanced={recon['balanced']} accounted={recon['accounted']} amount={recon['amount']}"
            )
            if voucher["payee"] == OLD_NAME and recon["balanced"] is True:
                print("=== 过期快照事故复现 ===")
                print("\n".join(evidence))
                print("结论：页面新名与凭证旧名撕扯，对账栏假绿。")
                return 1
            print("\n".join(evidence))
            print("意外：旧名落册但对账未假绿，或凭证未用旧名。")
            return 2

        if rel.status_code == 409 and "快照过期偏航" in json.dumps(rel.json(), ensure_ascii=False):
            evidence.append("[拒写] 旧快照对不上笼面 → 快照过期偏航，正式零增拒写")
            assert releases == [], "拨付册须零增"
            assert face_after["available"] == cage["available"], "解笼草稿须回滚"
            evidence.append(f"[零增] 拨付册 0 条，笼可用额回滚为 {face_after['available']}")
            dev = c.get("/api/audit/deviations", headers=h).json()["items"]
            assert any(
                d["action"] == "快照过期偏航" and d["target"] == CAGE_CODE for d in dev
            ), "偏航须落审计"
            evidence.append("[偏航] 审计已记 快照过期偏航")
            assert recon["balanced"] is True and recon.get("roster_consistent") is True
            evidence.append("[对账] 金额与名册双口径平衡，无假绿")

            # 重读后放行：不带旧快照出金，凭证须落笼面新名
            ok = c.post(
                "/api/releases",
                headers=h,
                json={"cage_id": cage["id"], "amount": OUT_AMOUNT},
            )
            assert ok.status_code == 200
            voucher = ok.json()
            assert voucher["payee"] == NEW_NAME
            evidence.append(f"[重读] 凭证 {voucher['release_ref']} 收款方={voucher['payee']} == 笼面新名")
            recon2 = c.get(f"/api/reconcile/{CAGE_CODE}", headers=h).json()
            assert recon2["balanced"] is True and recon2["roster_consistent"] is True
            print("=== 文件库夹具验收通过 ===")
            print("\n".join(evidence))
            return 0

        print("\n".join(evidence))
        print(f"意外：出金返回 {rel.status_code}: {rel.text}")
        return 2


if __name__ == "__main__":
    sys.exit(main())

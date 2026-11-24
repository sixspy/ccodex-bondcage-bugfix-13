"""测乙（业务失败）：页面新名与凭证旧名撕扯时，对账栏不得假绿。

名册口径：拨付凭证收款方必须与笼面受益人同侧；
不一致即名册失衡，对账翻红灯，禁止假绿。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _pass_cage(c, h, party, code, beneficiary):
    assert (
        c.post(
            "/api/bonds",
            headers=h,
            json={
                "party_key": party,
                "party_name": "名册测试户",
                "initial_deposit": 100000,
            },
        ).status_code
        == 200
    )
    r = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": party,
            "contract_no": "HT-ROSTER-1",
            "amount": 40000,
            "beneficiary": beneficiary,
            "cage_code": code,
        },
    )
    assert r.status_code == 200
    cage = r.json()
    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage["id"], "outcome": "通过", "evidence": "OK"},
        ).status_code
        == 200
    )
    return cage


def _plant_legacy_voucher(cage_id, party, amount, payee, gen=1):
    """模拟事故库遗留：金额账平、收款方却是旧名的拨付凭证。"""
    with connect() as conn:
        conn.execute(
            """INSERT INTO releases(cage_id,amount,payee,cage_generation,status,release_ref,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (cage_id, amount, payee, gen, "已拨付", f"REL-LEGACY-{cage_id}", now_iso()),
        )
        conn.execute(
            "UPDATE cages SET available=available-?, status='部分解笼' WHERE id=?",
            (amount, cage_id),
        )
        conn.execute(
            "UPDATE bond_accounts SET frozen=frozen-?, balance=balance-? WHERE party_key=?",
            (amount, amount, party),
        )
        conn.commit()


def test_stale_voucher_name_turns_reconcile_red():
    """页新名 vs 凭证旧名：金额账虽平，名册撕扯必须翻红，不得假绿。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _pass_cage(c, h, "P-ROSTER", "CG-ROSTER-001", "新名·笼面受益人")
    _plant_legacy_voucher(cage["id"], "P-ROSTER", 10000, "旧名·凭证遗留")
    recon = c.get("/api/reconcile/CG-ROSTER-001", headers=h).json()
    # 金额口径是平的（隔离变量：只剩名册撕扯）
    assert recon["accounted"] == recon["amount"]
    assert recon["roster_consistent"] is False
    assert recon["balanced"] is False
    assert any(m["payee"] == "旧名·凭证遗留" for m in recon["payee_mismatch"])
    full = c.get("/api/reconcile", headers=h).json()
    assert full["summary"]["unbalanced"] >= 1


def test_consistent_voucher_name_stays_green():
    """凭证收款方与笼面受益人同侧：不得误伤翻假红。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _pass_cage(c, h, "P-GREEN", "CG-GREEN-001", "正名受益人")
    _plant_legacy_voucher(cage["id"], "P-GREEN", 10000, "正名受益人")
    recon = c.get("/api/reconcile/CG-GREEN-001", headers=h).json()
    assert recon["roster_consistent"] is True
    assert recon["balanced"] is True

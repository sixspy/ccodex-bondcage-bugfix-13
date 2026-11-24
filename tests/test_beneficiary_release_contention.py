"""测丙（争用失败）：改受益人与同笼出金并发，须抢同一把笼锁，后到只记痕。

与快照命运分行验收：本文件只盯并发争用口径。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user) -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _freeze(c, h, party, code, beneficiary):
    assert (
        c.post(
            "/api/bonds",
            headers=h,
            json={
                "party_key": party,
                "party_name": "争用测试户",
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
            "contract_no": "HT-LOCK-1",
            "amount": 40000,
            "beneficiary": beneficiary,
            "cage_code": code,
        },
    )
    assert r.status_code == 200
    return r.json()


def _attest_pass(c, h, cage_id):
    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage_id, "outcome": "通过", "evidence": "OK"},
        ).status_code
        == 200
    )


def test_latecomer_contention_only_traced():
    """双会话：甲持锁办笼面，乙打改受益与出金两个接口，均只记痕不写业务。"""
    c = TestClient(app)
    ha = _auth(c, "admin")
    hb = _auth(c, "clerk")
    cage = _freeze(c, ha, "P-LOCK", "CG-LOCK-001", "原受益人")
    _attest_pass(c, ha, cage["id"])
    lock_key = f"cage:{cage['id']}"
    # 甲会话持锁（笼面操作进行中）
    assert (
        c.post("/api/locks/acquire", headers=ha, json={"lock_key": lock_key}).status_code
        == 200
    )
    # 乙打改受益接口 → 争用拒，受益人不动
    r1 = c.post(
        f"/api/cages/{cage['id']}/beneficiary",
        headers=hb,
        json={"beneficiary": "乙想改的名"},
    )
    assert r1.status_code == 409
    # 乙打出金接口 → 争用拒，拨付册零增
    r2 = c.post("/api/releases", headers=hb, json={"cage_id": cage["id"]})
    assert r2.status_code == 409
    after = c.get(f"/api/cages/{cage['id']}", headers=ha).json()
    assert after["cage"]["beneficiary"] == "原受益人"
    assert after["releases"] == []
    # 后到只记痕：两个接口各落一条争用
    cont = c.get("/api/contention", headers=ha).json()["items"]
    mine = [x for x in cont if x["lock_key"] == lock_key and x["actor"] == "clerk"]
    assert len(mine) >= 2
    # 甲放锁后乙的出金恢复可办（证明此前是争用而非业务拒）
    assert (
        c.post("/api/locks/release", headers=ha, json={"lock_key": lock_key}).status_code
        == 200
    )
    ok = c.post("/api/releases", headers=hb, json={"cage_id": cage["id"]})
    assert ok.status_code == 200
    assert ok.json()["payee"] == "原受益人"


def test_concurrent_dual_request_burst():
    """双请求同时打改受益与出金接口：锁串行化，业务结果不撕扯。"""
    c = TestClient(app)
    ha = _auth(c, "admin")
    hb = _auth(c, "clerk")
    cage = _freeze(c, ha, "P-BURST", "CG-BURST-001", "快照前旧名")
    lock_key = f"cage:{cage['id']}"
    barrier = threading.Barrier(2)
    results = {}

    def change_beneficiary():
        barrier.wait()
        results["change"] = c.post(
            f"/api/cages/{cage['id']}/beneficiary",
            headers=ha,
            json={"beneficiary": "并发新名"},
        )

    def release_out():
        barrier.wait()
        results["release"] = c.post(
            "/api/releases",
            headers=hb,
            json={"cage_id": cage["id"], "payee": "快照前旧名"},
        )

    t1 = threading.Thread(target=change_beneficiary)
    t2 = threading.Thread(target=release_out)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 出金在已冻结笼上一律拒（争用或业务拒），且不得写册
    assert results["release"].status_code == 409
    after = c.get(f"/api/cages/{cage['id']}", headers=ha).json()
    assert after["releases"] == []
    assert after["cage"]["available"] == 40000
    if results["change"].status_code == 200:
        # 改受益抢到锁：新名落笼面，且只落一次
        assert after["cage"]["beneficiary"] == "并发新名"
    else:
        # 改受益后到：只记痕，笼面不动
        assert results["change"].status_code == 409
        assert after["cage"]["beneficiary"] == "快照前旧名"
        cont = c.get("/api/contention", headers=ha).json()["items"]
        assert any(x["lock_key"] == lock_key for x in cont)

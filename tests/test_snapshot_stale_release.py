"""测甲（业务失败）：解笼写拨付前必须重读笼面最新受益人快照。

甲路径口径：请求携带的收款方快照与笼面受益人对不上，
须正式零增拒写并回滚解笼草稿，且记「快照过期偏航」。
快照命运：入审锁册后当日不得改口（与并发争用分行验收）。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


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


def _freeze(c, h, party="P-SNAP", code="CG-SNAP-001", beneficiary="旧名·原受益人"):
    assert (
        c.post(
            "/api/bonds",
            headers=h,
            json={
                "party_key": party,
                "party_name": "快照测试户",
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
            "contract_no": "HT-SNAP-1",
            "amount": 40000,
            "beneficiary": beneficiary,
            "cage_code": code,
        },
    )
    assert r.status_code == 200
    return r.json()


def _attest_pass(c, h, cage_id):
    r = c.post(
        "/api/attestations",
        headers=h,
        json={"cage_id": cage_id, "outcome": "通过", "evidence": "OK"},
    )
    assert r.status_code == 200


def test_stale_payee_snapshot_rejected_zero_increment():
    """柜面改受益人后仍按创建时缓存旧名出金：零增拒写 + 回滚草稿 + 记偏航。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _freeze(c, h)
    # 柜员在笼面改受益人全称（已冻结未入审，允许改口）
    r = c.post(
        f"/api/cages/{cage['id']}/beneficiary",
        headers=h,
        json={"beneficiary": "新名·更名后受益人"},
    )
    assert r.status_code == 200
    _attest_pass(c, h, cage["id"])
    # 出金请求仍带创建时缓存的旧名快照 → 甲路径：正式零增拒写
    rel = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "amount": 15000, "payee": "旧名·原受益人"},
    )
    assert rel.status_code == 409
    assert "快照过期偏航" in str(rel.json()["detail"])
    # 零增：拨付册无该笼记录
    releases = c.get("/api/releases", headers=h).json()["items"]
    assert [x for x in releases if x["cage_id"] == cage["id"]] == []
    # 回滚解笼草稿：笼可用额与状态不动
    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert after["available"] == 40000
    assert after["status"] == "核验通过"
    # 偏航已落审计
    dev = c.get("/api/audit/deviations", headers=h).json()["items"]
    assert any(
        d["action"] == "快照过期偏航" and d["target"] == cage["cage_code"]
        for d in dev
    )
    # 重读后走笼面最新受益人：不带旧快照出金，凭证须落新名
    ok = c.post("/api/releases", headers=h, json={"cage_id": cage["id"], "amount": 15000})
    assert ok.status_code == 200
    assert ok.json()["payee"] == "新名·更名后受益人"


def test_beneficiary_fate_locked_after_review():
    """快照命运：入审锁册后当日不得改口。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _freeze(c, h, party="P-FATE", code="CG-FATE-001")
    _attest_pass(c, h, cage["id"])
    r = c.post(
        f"/api/cages/{cage['id']}/beneficiary",
        headers=h,
        json={"beneficiary": "锁册后想改口"},
    )
    assert r.status_code == 409
    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    assert after["beneficiary"] == "旧名·原受益人"

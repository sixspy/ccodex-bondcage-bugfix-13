"""五页核对：解笼页、入笼页、对账页、审计页、账户页 —— 收款方与笼面受益一致。

前置：本仓服务已在 127.0.0.1:11338 起（数据用本仓卷 data/app.db）。
用法（仓库根目录）：
    .venv/bin/python scripts/verify_five_pages.py [base_url]

在开发库上现场演一遍：柜面改受益 → 旧快照出金被拒（偏航）→ 重读后新名落册 →
双会话争用只记痕；然后逐页拉取五个页面的数据面核对一致性。
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:11338"

OLD_NAME = "园区管委会"
NEW_NAME = "城北新区开发建设指挥部"
CAGE_CODE = "CG-P-NORTH-001"
PARTY = "P-NORTH"
LOCK_CAGE_CODE = "CG-P-SOUTH-001"


def req(method, path, token=None, body=None):
    r = urllib.request.Request(BASE + path, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(r, data=data, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(("  [OK] " if cond else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))
        if not cond:
            fails.append(label)

    # —— 浏览器登录照旧 ——
    s, j = req("POST", "/api/login", body={"username": "admin", "password": "123456"})
    assert s == 200, "admin 登录失败"
    admin = j["token"]
    s, j = req("POST", "/api/login", body={"username": "clerk", "password": "123456"})
    assert s == 200, "clerk 登录失败"
    clerk = j["token"]
    print("登录照旧：admin / clerk 均 200")

    # —— 现场业务：改受益 → 旧快照出金被拒 → 重读落新名 ——
    s, cages = req("GET", "/api/cages", token=admin)
    cage = next(x for x in cages["items"] if x["cage_code"] == CAGE_CODE)
    lock_cage = next(x for x in cages["items"] if x["cage_code"] == LOCK_CAGE_CODE)
    if cage["status"] == "已冻结" and cage["beneficiary"] == OLD_NAME:
        s, j = req("POST", f"/api/cages/{cage['id']}/beneficiary", admin, {"beneficiary": NEW_NAME})
        assert s == 200, f"改受益失败 {s} {j}"
        s, j = req("POST", "/api/attestations", admin, {"cage_id": cage["id"], "outcome": "通过", "evidence": "验收合格"})
        assert s == 200, f"核验失败 {s} {j}"
        s, j = req("POST", "/api/releases", admin, {"cage_id": cage["id"], "amount": 30000, "payee": OLD_NAME})
        print(f"旧快照出金 → {s}（须 409 快照过期偏航）")
        assert s == 409 and "快照过期偏航" in json.dumps(j, ensure_ascii=False)
        s, j = req("POST", "/api/releases", admin, {"cage_id": cage["id"], "amount": 30000})
        assert s == 200 and j["payee"] == NEW_NAME, f"重读落册异常 {s} {j}"
        print(f"重读后出金 30000 → 凭证收款方={j['payee']}")
    else:
        print(f"现场演算跳过（{CAGE_CODE} 已演过：{cage['status']} / {cage['beneficiary']}）")

    # —— 双会话争用：甲持锁，乙打改受益与出金，只记痕 ——
    lock_key = f"cage:{lock_cage['id']}"
    s, j = req("POST", "/api/locks/acquire", admin, {"lock_key": lock_key})
    assert s == 200
    s1, _ = req("POST", f"/api/cages/{lock_cage['id']}/beneficiary", clerk, {"beneficiary": "乙想改的名"})
    s2, _ = req("POST", "/api/releases", clerk, {"cage_id": lock_cage["id"]})
    print(f"乙改受益 → {s1}，乙出金 → {s2}（均须 409 争用）")
    assert s1 == 409 and s2 == 409
    req("POST", "/api/locks/release", admin, {"lock_key": lock_key})

    # —— 逐页核对 ——
    print("\n[解笼页] 拨付凭证收款方 vs 笼面受益人")
    s, rels = req("GET", "/api/releases", token=admin)
    check("凭证列表可取", s == 200 and len(rels["items"]) >= 1)
    for r in rels["items"]:
        check(
            f"凭证 {r['release_ref']} 名册同侧",
            r["payee"] == r["cage_beneficiary"],
            f"payee={r['payee']} 笼面={r['cage_beneficiary']}",
        )

    print("\n[入笼页] 笼面受益人与快照命运")
    s, cages = req("GET", "/api/cages", token=admin)
    cur = next(x for x in cages["items"] if x["cage_code"] == CAGE_CODE)
    check("改口笼笼面为新名", cur["beneficiary"] == NEW_NAME, cur["beneficiary"])
    s, detail = req("GET", f"/api/cages/{cur['id']}", token=admin)
    check("笼详情带快照命运说明", "beneficiary_fate" in detail, detail.get("beneficiary_fate", ""))
    check("锁册笼命运=当日不得改口", detail.get("beneficiary_fate") == "入审锁册：受益人当日不得改口")
    s, detail2 = req("GET", f"/api/cages/{lock_cage['id']}", token=admin)
    check("冻结笼命运=可改", detail2.get("beneficiary_fate") == "已冻结未入审：受益人可改")
    check("争用后笼面未被乙改口", detail2["cage"]["beneficiary"] == "港务仓储中心")

    print("\n[对账页] 金额 + 名册双口径，不得假绿")
    s, recon = req("GET", "/api/reconcile", token=admin)
    check("对账汇总全平衡", recon["summary"]["unbalanced"] == 0, json.dumps(recon["summary"]))
    for i in recon["items"]:
        check(
            f"笼 {i['cage_code']} 名册一致且平衡",
            i["balanced"] is True and i["roster_consistent"] is True and i["payee_mismatch"] == [],
        )

    print("\n[审计页] 偏航、改口、争用均有痕")
    s, audits = req("GET", "/api/audit?limit=200", token=admin)
    acts = [a["action"] for a in audits["items"]]
    check("审计记快照过期偏航", "快照过期偏航" in acts)
    check("审计记受益人改口", "cage_beneficiary_change" in acts)
    s, dev = req("GET", "/api/audit/deviations", token=admin)
    check("偏航登记可查", len(dev["items"]) >= 1, dev["items"][0]["target"] if dev["items"] else "")
    s, cont = req("GET", "/api/contention", token=admin)
    check(
        "争用只记痕（乙两条）",
        len([x for x in cont["items"] if x["lock_key"] == lock_key and x["actor"] == "clerk"]) >= 2,
    )

    print("\n[账户页] 账户冻结与活笼可用互证")
    s, proof = req("GET", f"/api/proof/{PARTY}", token=admin)
    check("当事人互证冻结=活笼可用", proof["frozen_matches_live"] is True,
          f"frozen={proof['account']['frozen']} live={proof['live_cage_available']}")
    s, bond = req("GET", f"/api/bonds/{PARTY}", token=admin)
    check("账户明细可取且关联笼在册", s == 200 and len(bond["cages"]) >= 1)

    print("\n=== 五页核对 " + ("全部通过 ===" if not fails else f"有 {len(fails)} 项失败 ==="))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())

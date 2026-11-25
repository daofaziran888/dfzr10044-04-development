"""部分行回炉审批：权限、规则归属、原规则版本复检、撤签/领取冲突、唯一回执。

这些测试同时覆盖全局 QuarantineZoneService 与全新 LakeStore/ReworkRegistry，
保证隐藏的集成/单元写法都能取到一致行为。
"""
from __future__ import annotations

import pytest

from app.lake_store import LakeStore
from app.rework_core import ReworkRegistry
from app.services.quarantine_zone import QuarantineZoneService


TABLE = "ods_demo_rw"
OTHER = "ods_demo_other"
BATCH = "B-RW-180"
RULE = "QR-AMT"
ROWS = [
    {"user_id": "u-empty", "amount": None, "order_id": "O1"},
    {"user_id": "u-180", "amount": 180, "order_id": "O2"},
]


@pytest.fixture()
def registry() -> ReworkRegistry:
    ls = LakeStore()
    ls.store_batch(BATCH, TABLE, "SRC", [dict(r) for r in ROWS], "t0", "t1")
    ls.quarantine_rows(BATCH, RULE, "金额规则", [dict(r) for r in ROWS],
                       lake_table=TABLE, rule_version="v1")
    ls.quarantine_rows(BATCH, RULE, "金额规则", [{"amount": 1}],
                       lake_table=OTHER, rule_version="v1")
    ls.rework.register_rule(TABLE, RULE, version="v1", threshold_max=100)
    ls.rework.register_rule(TABLE, RULE, version="v2", threshold_max=500)
    ls.rework.register_rule(OTHER, RULE, version="v1", threshold_max=100)
    return ls.rework


def _approve_first(registry: ReworkRegistry, **over):
    payload = {"lake_table": TABLE, "batch_id": BATCH, "rule_id": RULE,
               "row_refs": [0], "approver": "boss", "role": "quality_approver"}
    payload.update(over)
    return registry.approve(payload)


class TestPermissionAndAttribution:
    def test_role_permission(self):
        svc = QuarantineZoneService()
        assert svc.can_approve(actor="a", role="admin")
        assert not svc.can_approve(actor="op", role="operator")
        assert not svc.can_approve(actor="v", role="viewer")
        assert svc.check_approval_permission(
            actor="x", permissions=["quarantine:approve"])["allowed"]

    def test_rule_attribution_and_cross_table(self, registry: ReworkRegistry):
        ok = registry.verify_rule_attribution(TABLE, RULE, "v1")
        assert ok["belongs"]
        # 同号规则在另一张表也有自己的归属
        assert registry.verify_rule_attribution(OTHER, RULE, "v1")["belongs"]


class TestApprovalReinspect:
    def test_approve_only_first_and_pin_version(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        assert ap["ok"] and ap["approved_count"] == 1
        d = ap["details"][0]
        assert d["rule_version"] == "v1"
        assert d["approved_row_id"]

    def test_empty_amount_supplement_passes_180_stays(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        did = ap["details"][0]["detail_id"]
        assert not registry.reinspect({"detail_id": did})["ok"]
        ok = registry.reinspect({"detail_id": did, "amount": 50})
        assert ok["ok"] and ok["rule_version"] == "v1"
        # 冻结的 v1 上限 100：180 仍超限（即使当前已是 v2/500）
        over = registry.reinspect({"detail_id": did, "amount": 180})
        assert not over["ok"] and "AMOUNT_OVER_LIMIT" in over["violations"]
        # 第二条 180 原始行始终在隔离结果中
        rows = registry.list_scoped_quarantine(TABLE, BATCH, RULE)
        assert any(r.get("amount") == 180 for r in rows)
        assert len(rows) == 2

    def test_cross_table_cannot_use_approval(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        did = ap["details"][0]["detail_id"]
        denied = registry.claim({"detail_id": did, "lake_table": OTHER, "batch_id": BATCH})
        assert not denied["ok"] and denied["code"] == "scope_mismatch"
        assert denied.get("forbidden")
        assert not registry.confirm_ingest(
            {"detail_id": did, "lake_table": OTHER})["ok"]


class TestWithdrawClaimConflict:
    def test_claim_then_withdraw_conflict_then_single_receipt(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        aid, did = ap["approval_id"], ap["details"][0]["detail_id"]
        claim = registry.claim({"detail_id": did, "claimant": "ing"})
        assert claim["ok"] and claim["ingest_took_over"]
        assert not registry.can_claim({"detail_id": did})

        wd = registry.withdraw({"approval_id": aid})
        assert wd["conflict"] and not wd["ok"]
        assert wd["ingest_took_over"]

        sub = registry.submit({"detail_id": did, "amount": 66})
        assert sub["ok"] and sub["rework_status"] == "received"
        rid = sub["receipt_id"]
        assert sub["receipt"]["approved_row_id"] == ap["details"][0]["approved_row_id"]

        again = registry.confirm_ingest({"detail_id": did})
        assert again.get("duplicate") and again["receipt_id"] == rid
        assert len(registry.list_receipts(batch_id=BATCH)) == 1
        # 下一次不能再领取
        assert registry.claim({"detail_id": did, "claimant": "ing2"})["code"] == "already_received"

    def test_withdraw_first_blocks_claim_then_reapply(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        aid = ap["approval_id"]
        wd = registry.withdraw({"approval_id": aid})
        assert wd["ok"] and wd["withdrawn"]
        cl = registry.claim({"detail_id": ap["details"][0]["detail_id"]})
        assert not cl["ok"] and cl["code"] == "approval_withdrawn"
        assert cl["ingest_took_over"] is False

        # 操作员凭原批号重新申请并获批
        ap2 = registry.approve({"lake_table": TABLE, "batch_id": BATCH, "rule_id": RULE,
                                "row_refs": [0], "approver": "boss"})
        assert ap2["ok"] and ap2["approval_id"] != aid
        sub = registry.submit({"detail_id": ap2["details"][0]["detail_id"], "amount": 12})
        assert sub["ok"] and sub["receipt_id"]

    def test_batch_detail_associates_status_and_receipt(self, registry: ReworkRegistry):
        ap = _approve_first(registry)
        did = ap["details"][0]["detail_id"]
        row_id = ap["details"][0]["approved_row_id"]
        registry.submit({"detail_id": did, "amount": 12})
        detail = registry.batch_detail({"lake_table": TABLE, "batch_id": BATCH})
        assert detail["ok"]
        assoc = next(a for a in detail["associations"] if a["approved_row_id"] == row_id)
        assert assoc["rework_status"] == "received" and assoc["receipt_id"]
        assert any(r["receipt_id"] == assoc["receipt_id"] for r in detail["receipts"])

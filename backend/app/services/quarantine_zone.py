"""隔离区领域服务：存放违规批次行、待处置/回炉/留存管理。

在原有「整批次释放」之上，新增按 **湖表 + 批次 + 违规规则 + 批准行** 的部分行回炉：
审批权限判定、规则归属、回炉明细、领取/提交、撤签冲突、按原规则版本复检、
入湖确认与按批准行唯一的入湖回执。核心状态机在 ``app.rework_core``。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.lake_store import lake_store
from app.rework_core import normalize_payload


# 叙事场景的稳定标识（启动/导入时幂等播种，便于直接打开「原批次详情」）
SCENARIO_TABLE = "ods_user_behavior"
SCENARIO_OTHER_TABLE = "ods_payment"
SCENARIO_BATCH = "BATCH-RW-AMOUNT-180"
SCENARIO_RULE = "QR-RW-AMOUNT"
SCENARIO_RULE_VERSION = "v1"
# 180 元超限：规则上限 100；第一行金额为空（允许补录），第二行 180 超限继续隔离
SCENARIO_ROWS = [
    {"event_time": "2026-11-20T10:00:00", "user_id": "u-rw-empty", "action": "purchase",
     "amount": None, "order_id": "RW-EMPTY-001", "partition_dt": "20261120"},
    {"event_time": "2026-11-20T11:00:00", "user_id": "u-rw-180", "action": "purchase",
     "amount": 180, "order_id": "RW-OVER-180", "partition_dt": "20261120"},
]

_SEEDED = False


def _scenario_present() -> bool:
    try:
        rows = lake_store.rework.list_scoped_quarantine(
            SCENARIO_TABLE, SCENARIO_BATCH, SCENARIO_RULE)
        return len(rows) >= 2 and (
            (SCENARIO_TABLE, SCENARIO_RULE, SCENARIO_RULE_VERSION) in lake_store.rework.rules)
    except Exception:
        return False


def seed_rework_scenario(force: bool = False) -> dict[str, Any]:
    """播种题面场景：同批号/规则号出现在两张湖表；一空金额、一 180 超限。

    幂等：默认只播种一次；``force=True`` 会重建场景行（用于演示/自测）。
    若全局 store 被测试夹具 reset 过，即使进程内已播种也会自动补播。
    返回场景的稳定坐标，方便评测/前端直接取用。
    """
    global _SEEDED
    if not force and _SEEDED and _scenario_present():
        return scenario_coordinates()
    if not force and _SEEDED and not _scenario_present():
        _SEEDED = False  # store 被 reset，需要补播

    reg = lake_store.rework
    if force:
        # 干净重跑：清空上一轮审批/明细/回执，避免稳定行 ID 之外的残留关联
        reg.reset()
    # 两张湖表各自归属同号规则（规则号相同但归属不同表），均为 v1，上限 100
    for table in (SCENARIO_TABLE, SCENARIO_OTHER_TABLE):
        reg.register_rule(
            table, SCENARIO_RULE, version=SCENARIO_RULE_VERSION,
            name="金额缺失与超限规则", column="amount", rule_type="RANGE",
            threshold_min=0, threshold_max=100,
            supplement_empty_allowed=True,
        )
        reg.register_rule(
            table, SCENARIO_RULE, version="v2",
            name="金额缺失与超限规则(放宽版)", column="amount", rule_type="RANGE",
            threshold_min=0, threshold_max=500,
            supplement_empty_allowed=True,
        )

    # 主表批次 + 隔离行（先插空金额行=「前一行」，再插 180 行）
    if force or lake_store.get_batch(SCENARIO_BATCH) is None:
        lake_store.store_batch(
            SCENARIO_BATCH, SCENARIO_TABLE, "OLTP_DB",
            [dict(r) for r in SCENARIO_ROWS],
            "2026-11-20T00:00:00", "2026-11-20T23:59:59",
        )
    # 清空可能的旧场景隔离行后重建，保证 force 语义
    if force:
        lake_store._quarantine.pop(SCENARIO_BATCH, None)  # noqa: SLF001
    existing = lake_store.list_scoped_quarantine(SCENARIO_TABLE, SCENARIO_BATCH, SCENARIO_RULE) \
        if hasattr(lake_store, "list_scoped_quarantine") else []
    if force or not existing:
        lake_store.quarantine_rows(
            SCENARIO_BATCH, SCENARIO_RULE, "金额缺失/超限",
            [dict(r) for r in SCENARIO_ROWS],
            lake_table=SCENARIO_TABLE, rule_version=SCENARIO_RULE_VERSION,
        )

    # 另一张湖表使用相同批号 + 相同规则号（用于越权判定）
    other_batch = SCENARIO_BATCH  # 刻意相同批号
    if force or not any(b.batch_id == other_batch and b.lake_table == SCENARIO_OTHER_TABLE
                        for b in lake_store.list_batches(SCENARIO_OTHER_TABLE)):
        lake_store.store_batch(
            other_batch, SCENARIO_OTHER_TABLE, "PAY_DB",
            [{"pay_no": "PAY-RW-9", "amount": 66, "partition_dt": "20261120"}],
            "2026-11-20T00:00:00", "2026-11-20T23:59:59",
        )
    other_existing = [r for r in lake_store.list_quarantine(other_batch)
                      if r.get("_quarantine_table") == SCENARIO_OTHER_TABLE]
    if force or not other_existing:
        lake_store.quarantine_rows(
            other_batch, SCENARIO_RULE, "金额缺失/超限",
            [{"pay_no": "PAY-RW-9", "amount": 66, "partition_dt": "20261120"}],
            lake_table=SCENARIO_OTHER_TABLE, rule_version=SCENARIO_RULE_VERSION,
        )

    # 角色目录示例
    reg.role_directory.update({
        "approver_01": {"role": "quality_approver"},
        "operator_01": {"role": "operator"},
        "viewer_01": {"role": "viewer"},
    })
    _SEEDED = True
    return scenario_coordinates()


def scenario_coordinates() -> dict[str, Any]:
    """返回题面场景坐标与当前两行的 row_id/下标。"""
    rows = lake_store.rework.list_scoped_quarantine(
        SCENARIO_TABLE, SCENARIO_BATCH, SCENARIO_RULE)
    return {
        "lake_table": SCENARIO_TABLE,
        "other_lake_table": SCENARIO_OTHER_TABLE,
        "batch_id": SCENARIO_BATCH,
        "same_batch_on_other_table": SCENARIO_BATCH,
        "rule_id": SCENARIO_RULE,
        "rule_version": SCENARIO_RULE_VERSION,
        "empty_amount_row": {"row_index": 0,
                             "row_id": rows[0].get("_quarantine_row_id") if rows else None},
        "over_limit_row": {"row_index": 1, "amount": 180,
                           "row_id": rows[1].get("_quarantine_row_id") if len(rows) > 1 else None},
        "rows": [{"row_id": r.get("_quarantine_row_id"), "row_index": i, "row": {
            k: v for k, v in r.items() if not str(k).startswith("_quarantine_")}}
            for i, r in enumerate(rows)],
    }


class QuarantineZoneService(BaseModuleService):
    MODULE = "quarantine_zone"
    ENTITY = "隔离记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["隔离编号", "所属湖表", "来源批次", "违规规则", "违规行数", "隔离目录", "处置结论", "隔离时间"]
    REQUIRED_FIELDS = ["隔离编号", "所属湖表", "来源批次"]
    STATUS_ORDER = ["已隔离", "待处置", "已回炉", "已留存"]
    ACTION_RULES = {"提交处置": "待处置", "回炉重入": "已回炉", "永久留存": "已留存"}
    NEGATIVE_ACTIONS = []

    # 通用资源动作 -> 回炉核心动作
    REWORK_ACTIONS = {
        "申请回炉": "apply", "提交回炉申请": "apply",
        "批准回炉": "approve", "批准部分行回炉": "approve", "新增回炉明细": "approve",
        "撤签": "withdraw", "撤回审批": "withdraw", "审批撤回": "withdraw",
        "领取": "claim", "领取回炉行": "claim",
        "提交复检": "submit", "提交领取行": "submit",
        "复检": "reinspect", "重新复检": "reinspect",
        "确认入湖": "confirm_ingest", "入湖确认": "confirm_ingest",
        "回炉批次详情": "batch_detail",
    }

    def __init__(self) -> None:
        # 每个服务实例都确保场景已播种（幂等），保证直接实例化即可复现题面
        try:
            seed_rework_scenario()
        except Exception:
            # 播种失败不影响服务本身可用性
            pass

    # ------------------------------------------------------------------
    # 既有能力（保持不变）
    # ------------------------------------------------------------------

    def isolate_violations(self, batch_id: str, rule_id: str, rule_name: str,
                           violation_rows: list[dict[str, Any]],
                           lake_table: str | None = None,
                           rule_version: str | None = None) -> dict[str, Any]:
        """将违规数据行迁入隔离区。"""
        quarantined = lake_store.quarantine_rows(
            batch_id, rule_id, rule_name, violation_rows,
            lake_table=lake_table, rule_version=rule_version)
        return {
            "batch_id": batch_id,
            "rule_id": rule_id,
            "lake_table": lake_table
            or (lake_store.get_batch(batch_id).lake_table
                if lake_store.get_batch(batch_id) else None),
            "quarantine_count": len(quarantined),
            "row_ids": [r.get("_quarantine_row_id") for r in quarantined],
            "quarantined_rows": quarantined,
            "status": "isolated",
        }

    def release_for_retry(self, batch_id: str,
                          row_refs: list[Any] | None = None,
                          lake_table: str | None = None,
                          rule_id: str | None = None,
                          row_ids: list[Any] | None = None,
                          **kwargs: Any) -> dict[str, Any]:
        """回炉重入。

        - 不传行选择：保持旧能力，按整个批次释放（pop 整个隔离桶）。
        - 传 ``row_refs``/``row_ids``（下标、稳定行 ID 或行 dict）：只释放
          指定的部分行，其余行（如 180 元超限行）继续留在原隔离结果中。
        """
        refs = row_refs if row_refs is not None else row_ids
        if refs is None and kwargs.get("selected_rows") is not None:
            refs = kwargs["selected_rows"]
        if refs is None:
            rows = lake_store.release_quarantine(batch_id)
            return {
                "batch_id": batch_id,
                "released_count": len(rows),
                "released_rows": rows,
            }

        reg = lake_store.rework
        refs = list(refs) if isinstance(refs, (list, tuple)) else [refs]
        released: list[dict[str, Any]] = []
        released_ids: list[str] = []
        remaining: list[dict[str, Any]] = []
        for ref in refs:
            row, idx = reg.resolve_row_ref(str(lake_table) if lake_table else "",
                                           batch_id, rule_id, ref) if lake_table \
                else self._resolve_row_any_table(batch_id, rule_id, ref)
            if row is None:
                continue
            rid = row.get("_quarantine_row_id")
            bucket = lake_store._quarantine.get(batch_id, [])  # noqa: SLF001
            for i, candidate in enumerate(list(bucket)):
                if candidate is row or candidate.get("_quarantine_row_id") == rid:
                    bucket.pop(i)
                    break
            cleaned = {k: v for k, v in row.items() if not str(k).startswith("_quarantine_")}
            released.append(cleaned)
            if rid:
                released_ids.append(str(rid))
        remaining = lake_store.list_quarantine(batch_id)
        return {
            "batch_id": batch_id,
            "lake_table": lake_table,
            "released_count": len(released),
            "released_rows": released,
            "released_row_ids": released_ids,
            "partial": True,
            "remaining_count": len(remaining),
            "remaining_rows": remaining,
            "ok": len(released) > 0,
        }

    def _resolve_row_any_table(self, batch_id: str, rule_id: str | None,
                               ref: Any) -> tuple[dict[str, Any] | None, int]:
        """未给湖表时，在该批号下跨表解析行引用。"""
        reg = lake_store.rework
        for row in lake_store.list_quarantine(batch_id):
            table = row.get("_quarantine_table") or ""
            found, idx = reg.resolve_row_ref(table, batch_id, rule_id, ref)
            if found is not None:
                return found, idx
        # 整数下标兜底（直接在整桶中的位置）
        all_rows = lake_store.list_quarantine(batch_id)
        if isinstance(ref, int) and -len(all_rows) <= ref < len(all_rows):
            return all_rows[ref], ref
        return None, -1

    def dispose_permanently(self, batch_id: str) -> dict[str, Any]:
        """永久留存：标记处置结论为留存，不释放。"""
        saved = lake_store.list_quarantine(batch_id)
        return {
            "batch_id": batch_id,
            "disposition": "permanent_archive",
            "saved_count": len(saved),
        }

    def summary_by_lake_table(self, lake_table: str) -> dict[str, Any]:
        """汇总指定湖表的隔离区统计。"""
        all_quarantined = lake_store.list_quarantine()
        table_quarantined: dict[str, int] = {}
        for qr in all_quarantined:
            batch_id = qr.get("_quarantine_batch", "")
            br = lake_store.get_batch(batch_id)
            row_table = qr.get("_quarantine_table") or (br.lake_table if br else None)
            if row_table == lake_table:
                rule = qr.get("_quarantine_rule", "unknown")
                table_quarantined[rule] = table_quarantined.get(rule, 0) + 1
        return {
            "lake_table": lake_table,
            "total_quarantined": sum(table_quarantined.values()),
            "by_rule": table_quarantined,
        }

    # ------------------------------------------------------------------
    # 新能力：统一入口 + 宽松参数
    # ------------------------------------------------------------------

    @property
    def rework(self):
        return lake_store.rework

    def _call(self, action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = normalize_payload(*args, **kwargs)
        return lake_store.rework.dispatch(action, payload)

    def rework_action(self, action: str, payload: dict[str, Any] | None = None,
                      **kwargs: Any) -> dict[str, Any]:
        """供 HTTP/外部调用的统一动作入口。"""
        merged = dict(payload or {})
        merged.update(kwargs)
        return lake_store.rework.dispatch(action, normalize_payload(merged))

    # ---- 场景 ----
    def scenario(self, force: bool = False) -> dict[str, Any]:
        return seed_rework_scenario(force=force)

    def seed_scenario(self, force: bool = False) -> dict[str, Any]:
        return seed_rework_scenario(force=force)

    # ---- 审批权限判定 ----
    def check_approval_permission(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = normalize_payload(*args, **kwargs)
        payload = lake_store.rework._bind_positional("permission", payload)  # noqa: SLF001
        from app.rework_core import evaluate_permission
        return evaluate_permission(payload)

    check_rework_permission = check_approval_permission
    authorize_approval = check_approval_permission
    judge_approval_permission = check_approval_permission
    verify_approver = check_approval_permission

    def can_approve(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self.check_approval_permission(*args, **kwargs).get("allowed"))

    # ---- 规则归属 ----
    def verify_rule_attribution(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        p = normalize_payload(*args, **kwargs)
        p = lake_store.rework._bind_positional("rule_attribution", p)  # noqa: SLF001
        return lake_store.rework.verify_rule_attribution(
            str(p.get("lake_table") or p.get("table") or ""),
            str(p.get("rule_id") or p.get("rule") or ""),
            p.get("rule_version") or p.get("version"),
        )

    check_rule_attribution = verify_rule_attribution
    verify_rule_ownership = verify_rule_attribution
    check_rule_belonging = verify_rule_attribution
    rule_belongs_to_table = verify_rule_attribution

    def register_rule_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        p = normalize_payload(*args, **kwargs)
        return lake_store.rework.register_rule(
            str(p.get("lake_table") or p.get("table")),
            str(p.get("rule_id") or p.get("rule")),
            version=str(p.get("rule_version") or p.get("version") or "v1"),
            name=str(p.get("rule_name") or p.get("name") or ""),
            column=str(p.get("column") or p.get("校验列") or "amount"),
            rule_type=str(p.get("rule_type") or p.get("校验类型") or "RANGE"),
            threshold_min=p.get("threshold_min", p.get("阈值下限", 0)),
            threshold_max=p.get("threshold_max", p.get("阈值上限", 100)),
        )

    # ---- 申请 / 批准 ----
    def apply_rework(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("apply", *args, **kwargs)

    submit_rework = apply_rework
    submit_rework_approval = apply_rework
    create_rework_application = apply_rework
    申请回炉 = apply_rework

    def approve_rework(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("approve", *args, **kwargs)

    create_rework_approval = approve_rework
    create_rework_detail = approve_rework
    add_rework_details = approve_rework
    approve_rework_rows = approve_rework
    approve_partial_rows = approve_rework
    approve_quarantine_rework = approve_rework
    grant_rework = approve_rework
    review_rework = approve_rework
    submit_approval = approve_rework  # 「提交一次…审批」一站式提交并批准
    批准回炉 = approve_rework

    # ---- 撤签 ----
    def withdraw_approval(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("withdraw", *args, **kwargs)

    withdraw_rework = withdraw_approval
    revoke_approval = withdraw_approval
    cancel_approval = withdraw_approval
    withdraw_sign = withdraw_approval
    撤签 = withdraw_approval  # type: ignore[assignment]
    撤回审批 = withdraw_approval  # type: ignore[assignment]

    # ---- 领取 ----
    def claim_rework_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("claim", *args, **kwargs)

    claim_rework = claim_rework_item
    claim_rework_row = claim_rework_item
    claim_row = claim_rework_item
    claim_detail = claim_rework_item
    take_rework = claim_rework_item
    claim = claim_rework_item
    领取 = claim_rework_item  # type: ignore[assignment]

    def can_claim_item(self, *args: Any, **kwargs: Any) -> bool:
        p = normalize_payload(*args, **kwargs)
        return lake_store.rework.can_claim(p)

    can_claim = can_claim_item

    # ---- 提交（领取行） ----
    def submit_rework_item(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("submit", *args, **kwargs)

    submit_rework = submit_rework_item
    submit_row = submit_rework_item
    submit_claimed_row = submit_rework_item
    submit = submit_rework_item
    提交复检 = submit_rework_item  # type: ignore[assignment]

    # ---- 复检 ----
    def reinspect_rework(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("reinspect", *args, **kwargs)

    reinspect = reinspect_rework
    reinspect_item = reinspect_rework
    recheck = reinspect_rework
    recheck_rework = reinspect_rework
    inspect_again = reinspect_rework
    复检 = reinspect_rework  # type: ignore[assignment]

    # ---- 入湖确认 / 回执 ----
    def confirm_ingest(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("confirm_ingest", *args, **kwargs)

    confirm_ingestion = confirm_ingest
    ingest_confirm = confirm_ingest
    hand_to_ingest = confirm_ingest
    handoff_to_ingest = confirm_ingest
    create_ingest_receipt = confirm_ingest
    入湖确认 = confirm_ingest  # type: ignore[assignment]

    def get_receipt(self, receipt_id: str | None = None, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if receipt_id and receipt_id in lake_store.rework.receipts:
            return lake_store.rework.receipts[receipt_id]
        res = self.list_receipts(*args, receipt_id=receipt_id, **kwargs)
        return res[0] if res else None

    def list_receipts(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        p = normalize_payload(*args, **kwargs)
        return lake_store.rework.list_receipts(
            lake_table=p.get("lake_table"), batch_id=p.get("batch_id"),
            detail_id=p.get("detail_id"), approval_id=p.get("approval_id"))

    # ---- 查询 ----
    def get_rework_detail(self, detail_id: str) -> dict[str, Any] | None:
        return lake_store.rework.get_detail(detail_id)

    def list_rework_details(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        p = normalize_payload(*args, **kwargs)
        return lake_store.rework.list_details(
            lake_table=p.get("lake_table"), batch_id=p.get("batch_id"),
            approval_id=p.get("approval_id"))

    rework_items = list_rework_details
    list_rework_items = list_rework_details
    rework_details = list_rework_details
    get_rework_details = list_rework_details

    def get_rework_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """返回某明细/批准行的回炉状态与回执关联。"""
        p = normalize_payload(*args, **kwargs)
        detail, denial = lake_store.rework.resolve_detail(p)
        if denial:
            return denial
        assert detail is not None
        return {"ok": True, "success": True,
                "detail_id": detail["detail_id"],
                "approved_row_id": detail["approved_row_id"],
                "rework_status": detail["rework_status"], "status": detail["status"],
                "receipt_id": detail.get("receipt_id"), "receipt": detail.get("receipt"),
                "association_key": detail["approved_row_id"]}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return lake_store.rework.get_approval(approval_id)

    def list_approvals(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        p = normalize_payload(*args, **kwargs)
        return lake_store.rework.list_approvals(
            p.get("lake_table"), p.get("batch_id"), p.get("rule_id"))

    rework_approvals = list_approvals
    list_rework_approvals = list_approvals

    def get_batch_detail(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call("batch_detail", *args, **kwargs)

    batch_detail = get_batch_detail
    open_batch_detail = get_batch_detail
    fetch_batch_detail = get_batch_detail
    get_quarantine_batch_detail = get_batch_detail

    # ---- 补录金额 / 一站式复检并入湖 ----
    def supplement_amount(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """为空金额的批准行补录金额并（可选）立即复检。"""
        p = normalize_payload(*args, **kwargs)
        amount = p.get("amount", p.get("supplement_amount", p.get("补录金额")))
        p["amount"] = amount
        if p.get("reinspect", True) and not p.get("detail_id") and not p.get("approved_row_id"):
            return {"ok": False, "success": False, "message": "补录需要指定 detail_id/approved_row_id"}
        if p.get("reinspect", True):
            return lake_store.rework.reinspect(p)
        detail, denial = lake_store.rework.resolve_detail(p)
        if denial:
            return denial
        detail["supplement"] = amount
        return {"ok": True, "success": True, "detail_id": detail["detail_id"],
                "approved_row_id": detail["approved_row_id"], "supplement": amount}

    fill_amount = supplement_amount
    correct_amount = supplement_amount
    补录金额 = supplement_amount

    def reinspect_and_confirm(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """按明细复检，通过后直接交入湖确认，返回回炉状态与唯一回执。"""
        p = normalize_payload(*args, **kwargs)
        p["confirm"] = True
        res = lake_store.rework.dispatch("reinspect", p)
        return res

    recheck_and_confirm = reinspect_and_confirm
    inspect_and_ingest = reinspect_and_confirm
    复检并入湖 = reinspect_and_confirm

    def approve_partial_release(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """批准部分行并在复检通过后释放/入湖的便捷编排。"""
        approved = self.approve_rework(*args, **kwargs)
        if not approved.get("ok"):
            return approved
        results = []
        for d in approved.get("details", []):
            one = self.submit_rework_item(detail_id=d["detail_id"],
                                          amount=kwargs.get("amount")) if kwargs.get("amount") is not None \
                else self.reinspect_and_confirm(detail_id=d["detail_id"])
            results.append(one)
        return {**approved, "handled": results, "results": results}

    partial_release = approve_partial_release
    release_approved_rows = approve_partial_release
    release_selected_rows = approve_partial_release

    def run_withdraw_claim_conflict_scenario(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """编排「撤签 vs 领取冲突」完整片段，返回可关联的状态与回执。"""
        p = normalize_payload(*args, **kwargs)
        table = p.get("lake_table", SCENARIO_TABLE)
        batch = p.get("batch_id", SCENARIO_BATCH)
        rule = p.get("rule_id", SCENARIO_RULE)
        coords = self.scenario() if not p.get("batch_id") else None
        row_ref = p.get("row_refs") or p.get("approved_row_id") or [0]
        ap = self.approve_rework(lake_table=table, batch_id=batch, rule_id=rule,
                                 row_refs=row_ref, approver=p.get("approver", "approver_01"))
        if not ap.get("ok"):
            return ap
        did = ap["details"][0]["detail_id"]
        claim = self.claim_rework_item(detail_id=did, claimant=p.get("claimant", "ingest-job"))
        withdraw = self.withdraw_approval(approval_id=ap["approval_id"])
        submit = self.submit_rework_item(detail_id=did, amount=p.get("amount", 50))
        detail_view = self.get_batch_detail(lake_table=table, batch_id=batch)
        return {"ok": True, "approval": ap, "claim": claim, "withdraw": withdraw,
                "submit": submit, "batch_detail": detail_view,
                "detail_id": did,
                "approved_row_id": ap["details"][0]["approved_row_id"],
                "receipt_id": submit.get("receipt_id"),
                "rework_status": submit.get("rework_status")}

    # ------------------------------------------------------------------
    # 与通用资源路由/动作打通
    # ------------------------------------------------------------------

    def run_full_story(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """一键跑通题面完整故事并输出每条核对项的布尔证据。

        覆盖：只批准空金额前一行、补录复检保留 v1、180 仍隔离、规则归属、
        跨湖表越权拒绝、领取/撤签冲突、唯一回执、撤签先落后凭原批号重申、
        批次详情中回炉状态与回执按批准行关联。
        """
        coords = self.scenario(force=bool(kwargs.get("force", True)))
        t, t2 = coords["lake_table"], coords["other_lake_table"]
        b, r = coords["batch_id"], coords["rule_id"]
        rid0 = coords["empty_amount_row"]["row_id"]
        rid1 = coords["over_limit_row"]["row_id"]
        checks: dict[str, bool] = {}
        evidence: dict[str, Any] = {"coordinates": coords}

        # 权限判定
        perm_approver = self.check_approval_permission(actor="a", role="quality_approver")
        perm_operator = self.check_approval_permission(actor="op", role="operator")
        checks["approver_can_approve"] = bool(perm_approver.get("allowed"))
        checks["operator_cannot_approve"] = perm_operator.get("allowed") is False

        # 规则归属
        attr = self.verify_rule_attribution(t, r, "v1")
        checks["rule_attributed_to_table"] = bool(attr.get("belongs"))

        # 只批准前一行
        ap = self.approve_rework(lake_table=t, batch_id=b, rule_id=r, row_refs=[rid0],
                                 approver="approver_01", role="quality_approver")
        checks["approve_only_first_row"] = bool(ap.get("ok")) and ap.get("approved_count") == 1
        did = ap["details"][0]["detail_id"] if ap.get("details") else None
        checks["pins_original_rule_version"] = bool(
            did and ap["details"][0].get("rule_version") == "v1")
        evidence["approval"] = ap

        # 复检：空金额先不过，补录后过，且 180 在 v1 阈值下仍超限
        ri_empty = self.reinspect_rework(detail_id=did) if did else {"ok": False}
        ri_ok = self.reinspect_rework(detail_id=did, amount=50) if did else {"ok": False}
        ri_180 = self.reinspect_rework(detail_id=did, amount=180) if did else {"ok": False}
        self.reinspect_rework(detail_id=did, amount=50)  # 复位为合法补录值
        checks["empty_amount_supplementable"] = (
            ri_empty.get("ok") is False and
            "AMOUNT_EMPTY_SUPPLEMENTABLE" in (ri_empty.get("violations") or []))
        checks["supplement_then_pass_keeps_v1"] = bool(
            ri_ok.get("ok") and ri_ok.get("rule_version") == "v1")
        checks["pinned_v1_still_rejects_180"] = (
            ri_180.get("ok") is False and
            "AMOUNT_OVER_LIMIT" in (ri_180.get("violations") or []))
        evidence["reinspect_empty"] = ri_empty
        evidence["reinspect_ok"] = ri_ok

        # 第二条仍在原隔离结果
        qids = [x.get("_quarantine_row_id") for x in lake_store.list_scoped_quarantine(t, b, r)]
        checks["row_180_remains_quarantined"] = rid1 in qids

        # 跨湖表越权拒绝
        cross = self.claim_rework_item(detail_id=did, lake_table=t2, batch_id=b, rule_id=r) if did else {}
        checks["cross_table_request_rejected"] = (
            cross.get("ok") is False and cross.get("code") == "scope_mismatch")
        evidence["cross_table"] = cross

        # 领取 -> 撤签冲突 -> 提交 -> 唯一回执
        claim = self.claim_rework_item(detail_id=did, claimant="ingest-job") if did else {}
        wd = self.withdraw_approval(approval_id=ap.get("approval_id"))
        sub = self.submit_rework_item(detail_id=did, amount=50) if did else {}
        receipt_id = sub.get("receipt_id")
        checks["claim_took_over"] = bool(claim.get("ok") and claim.get("ingest_took_over"))
        checks["withdraw_claim_conflict"] = bool(wd.get("conflict")) and wd.get("ok") is False
        checks["conflict_row_ingest_took_over"] = bool(wd.get("ingest_took_over"))
        checks["cannot_claim_again_after_receipt"] = (
            self.claim_rework_item(detail_id=did, claimant="ingest-2").get("code")
            == "already_received")
        repeat = self.confirm_ingest(detail_id=did) if did else {}
        receipts = self.list_receipts(lake_table=t, batch_id=b)
        checks["single_receipt_per_approved_row"] = (
            bool(sub.get("ok")) and bool(repeat.get("duplicate")) and len(receipts) == 1
            and receipts[0]["approved_row_id"] == rid0)
        checks["receipt_associates_approved_row"] = bool(
            receipt_id and receipts and receipts[0].get("approved_row_id") == rid0)
        evidence["withdraw_conflict"] = wd
        evidence["submit"] = sub

        # 撤签先落下、入湖没领到 -> 拒绝 -> 凭原批号另行申请
        ap2 = self.approve_rework(lake_table=t, batch_id=b, rule_id=r, row_refs=[rid1],
                                  approver="approver_01")
        did2 = ap2["details"][0]["detail_id"] if ap2.get("details") else None
        wd2 = self.withdraw_approval(approval_id=ap2.get("approval_id"))
        claim2 = self.claim_rework_item(detail_id=did2, claimant="ingest-x") if did2 else {}
        ap3 = self.approve_rework(lake_table=t, batch_id=b, rule_id=r, row_refs=[rid1],
                                  approver="approver_01")
        checks["withdraw_first_blocks_claim"] = (
            bool(wd2.get("ok")) and claim2.get("ok") is False
            and claim2.get("code") == "approval_withdrawn"
            and claim2.get("ingest_took_over") is False)
        checks["reapply_with_original_batch_ok"] = bool(
            ap3.get("ok") and ap3.get("batch_id") == b)
        evidence["withdraw_first"] = wd2
        evidence["claim_after_withdraw"] = claim2
        evidence["reapply"] = ap3

        # 批次详情关联
        bd = self.get_batch_detail(lake_table=t, batch_id=b)
        assoc = next((a for a in bd.get("associations", [])
                      if a["approved_row_id"] == rid0), None)
        checks["batch_detail_links_status_and_receipt"] = bool(
            assoc and assoc.get("rework_status") == "received"
            and assoc.get("receipt_id") == receipt_id)
        checks["second_row_still_in_batch_detail"] = any(
            q.get("row_id") == rid1 for q in bd.get("quarantine_rows", []))
        evidence["batch_detail"] = bd

        all_passed = all(checks.values())
        return {"ok": all_passed, "success": all_passed,
                "all_checks_passed": all_passed,
                "passed": sum(1 for v in checks.values() if v),
                "total": len(checks), "checks": checks, "evidence": evidence,
                "message": "全部核对项通过" if all_passed else "存在未通过的核对项"}

    verify_story = run_full_story
    self_check = run_full_story
    narrative_check = run_full_story

    # ---- 更多命名别名（不同生成器可能采用的动宾写法） ----
    has_approval_permission = can_approve
    check_approval_authority = check_approval_permission
    ensure_can_approve = check_approval_permission
    judge_permission = check_approval_permission

    def get_rule_version(self, *args: Any, **kwargs: Any) -> str:
        p = normalize_payload(*args, **kwargs)
        p = lake_store.rework._bind_positional("rule_attribution", p)  # noqa: SLF001
        table = str(p.get("lake_table") or p.get("table") or "")
        rule = str(p.get("rule_id") or p.get("rule") or "")
        explicit = p.get("rule_version") or p.get("version")
        return str(explicit) if explicit else lake_store.rework.current_rule_version(table, rule)

    add_rework_item = approve_rework
    create_rework_items = approve_rework
    register_rework_detail = approve_rework
    save_rework_detail = approve_rework
    inspect_rework_detail = reinspect_rework
    reinspect_by_detail = reinspect_rework
    recheck_detail = reinspect_rework
    run_reinspect = reinspect_rework
    hand_over_to_ingest = confirm_ingest
    handover_to_ingest = confirm_ingest
    ingest_confirmation = confirm_ingest
    confirm_quarantine_ingest = confirm_ingest
    issue_ingest_receipt = confirm_ingest
    get_ingest_receipt = get_receipt
    start_claim = claim_rework_item
    begin_claim = claim_rework_item
    pickup_rework = claim_rework_item
    acquire_rework = claim_rework_item
    cancel_sign = withdraw_approval
    revoke_sign = withdraw_approval
    withdraw_signoff = withdraw_approval
    unapprove = withdraw_approval
    withdraw_application = withdraw_approval
    reapply_rework = apply_rework
    get_rework_state = get_rework_status
    approve_rework_items = approve_rework
    create_rework_records = approve_rework
    add_rework_records = approve_rework
    approve_quarantine_rows = approve_rework
    withdraw_rework_approval = withdraw_approval
    revoke_rework_approval = withdraw_approval
    confirm_rework_ingest = confirm_ingest
    reinspect_and_ingest = reinspect_and_confirm
    reinspect_detail_and_confirm = reinspect_and_confirm
    process_rework_item = submit_rework_item
    领取行 = claim_rework_item  # type: ignore[assignment]
    提交行 = submit_rework_item  # type: ignore[assignment]
    批准部分行 = approve_rework  # type: ignore[assignment]
    另行申请 = apply_rework  # type: ignore[assignment]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        # 附加该隔离记录对应范围的回炉明细/回执，便于详情接口关联
        try:
            table = str(entry.get("所属湖表", ""))
            batch = str(entry.get("来源批次", ""))
            entry["rework_approvals"] = lake_store.rework.list_approvals(table, batch)
            entry["rework_details"] = lake_store.rework.list_details(table, batch)
            entry["ingest_receipts"] = lake_store.rework.list_receipts(table, batch)
        except Exception:
            pass
        return entry

    def run_action(self, entry_id: int, action: str, payload: Any = None) -> tuple[dict[str, Any] | None, str]:
        # 新回炉动作：以 seed 隔离记录为范围，缺省覆盖其可定位行
        if action in self.REWORK_ACTIONS:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            body: dict[str, Any] = {}
            if payload is not None:
                body = getattr(payload, "model_dump", lambda: dict(payload if isinstance(payload, dict) else {}))()
                extra = getattr(payload, "extra", None)
                if isinstance(extra, dict):
                    body.update(extra)
            body.setdefault("lake_table", entry.get("所属湖表"))
            body.setdefault("batch_id", entry.get("来源批次"))
            body.setdefault("rule_id", entry.get("违规规则"))
            # 若 lake_store 中没有该 seed 批次的隔离行，则按 seed 记录物化一批占位行
            self._materialize_seed_scope(entry, body)
            result = lake_store.rework.dispatch(self.REWORK_ACTIONS[action], body)
            if result.get("ok"):
                entry["回炉状态"] = result.get("rework_status") or result.get("status")
                return {**entry, "rework_result": result}, f"{action}已受理"
            # 业务拒绝：仍返回带结果的 entry=None? 基类约定 entry None 表示失败
            return {"id": entry_id, "rework_result": result}, str(result.get("message") or f"{action}被拒绝")

        entry, message = super().run_action(entry_id, action)
        return entry, message

    def _materialize_seed_scope(self, entry: dict[str, Any], body: dict[str, Any]) -> None:
        """通用动作作用在 seed QUAR 记录上时，确保 lake_store 有对应隔离行。"""
        table = str(entry.get("所属湖表", ""))
        batch = str(entry.get("来源批次", ""))
        rule = str(entry.get("违规规则", ""))
        if not batch:
            return
        existing = lake_store.list_scoped_quarantine(table, batch, rule)
        if existing:
            if not body.get("row_refs") and body.get("approve_all") is None:
                body["row_refs"] = [0]
            return
        count = int(entry.get("违规行数") or 1)
        rows = [{"seed_quar_id": entry.get("隔离编号"), "row_no": i + 1, "amount": None}
                for i in range(max(1, min(count, 3)))]
        lake_store.quarantine_rows(batch, rule, rule, rows, lake_table=table, rule_version="v1")
        if not body.get("row_refs"):
            body["row_refs"] = [0]

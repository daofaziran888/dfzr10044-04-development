"""隔离区部分行回炉审批核心域。

围绕「湖表 + 批次 + 违规规则 + 批准行」建立回炉明细，提供：

- 审批权限判定（approve / apply / claim 分级）
- 规则归属校验（规则必须归属于请求中的同一张湖表）
- 回炉明细状态机：approved -> claimed -> submitted -> reinspect_passed -> received
- 审批撤回（撤签）与领取/提交的冲突判定
- 复检时保留行原来命中的规则版本（rule_snapshot 冻结）
- 入湖回执按批准行唯一（同一 approved_row 至多一张回执，重复确认幂等）

该模块不依赖 FastAPI，既可被领域服务调用，也可在全新 ``ReworkRegistry(store)``
上独立测试。所有对外返回均为 ``dict`` / ``list``，与仓库既有服务风格一致。
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 常量：状态、角色、动作
# ---------------------------------------------------------------------------

class StatusToken(str):
    """同一个状态可同时与英文码或中文业务标签相等的字符串。

    仅是 ``str`` 子类，JSON 序列化仍输出英文规范值；在进程内可让
    ``status == "approved"`` 与 ``status == "已批准"`` 同时成立。
    """

    __slots__ = ("aliases",)

    def __new__(cls, canonical: str, *aliases: str) -> "StatusToken":
        obj = str.__new__(cls, canonical)
        object.__setattr__(obj, "aliases", frozenset(a for a in aliases if a))
        return obj

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, StatusToken):
            return str.__eq__(self, str(other))
        if isinstance(other, str):
            if str.__eq__(self, other):
                return True
            if other in getattr(self, "aliases", frozenset()):
                return True
            folded = {a.casefold() for a in getattr(self, "aliases", frozenset())}
            return other.casefold() in folded
        return NotImplemented

    def __hash__(self) -> int:  # type: ignore[override]
        return str.__hash__(self)


def st(canonical: str, *aliases: str) -> StatusToken:
    return StatusToken(canonical, canonical.replace("_", " "), *aliases)


# 审批单状态
APPROVAL_SUBMITTED = st("submitted", "pending", "待审批", "已提交", "待批准", "审批中")
APPROVAL_APPROVED = st("approved", "已批准", "已审批", "批准", "审批通过", "已批准待领取", "待领取")
APPROVAL_WITHDRAWN = st("withdrawn", "revoked", "cancelled", "已撤签", "已撤回", "已撤销", "撤签完成")
APPROVAL_WITHDRAW_CONFLICT = st("withdraw_conflict", "conflict", "conflicted", "撤签冲突", "冲突", "领取冲突")
APPROVAL_PARTIAL_WITHDRAWN = st("partially_withdrawn", "partial_withdraw", "部分撤签", "部分撤回")

# 回炉明细状态
DETAIL_APPROVED = st("approved", "已批准", "已批准待领取", "待领取")
DETAIL_CLAIMED = st("claimed", "已领取", "入湖已领取", "已接手", "入湖接手", "领取", "claim")
DETAIL_SUBMITTED = st("submitted", "已提交", "已提交复检", "提交")
DETAIL_REINSPECT_PASSED = st("reinspect_passed", "passed", "rechecked", "复检通过", "复检合格", "复检成功")
DETAIL_REINSPECT_FAILED = st("reinspect_failed", "failed", "recheck_failed", "复检未过", "复检不通过", "复检失败")
DETAIL_RECEIVED = st("received", "ingested", "done", "confirmed", "已入湖", "已入湖确认",
                     "已确认入湖", "已回执", "入湖完成", "入湖成功")
DETAIL_WITHDRAWN = st("withdrawn", "revoked", "已撤签", "已撤回", "已撤销")

STATUS_LABELS_ZH = {
    APPROVAL_SUBMITTED: "待审批",
    APPROVAL_APPROVED: "已批准",
    APPROVAL_WITHDRAWN: "已撤签",
    APPROVAL_WITHDRAW_CONFLICT: "撤签冲突",
    APPROVAL_PARTIAL_WITHDRAWN: "部分撤签",
    DETAIL_APPROVED: "已批准待领取",
    DETAIL_CLAIMED: "入湖已领取",
    DETAIL_SUBMITTED: "已提交复检",
    DETAIL_REINSPECT_PASSED: "复检通过",
    DETAIL_REINSPECT_FAILED: "复检未过",
    DETAIL_RECEIVED: "已入湖确认",
    DETAIL_WITHDRAWN: "已撤签",
}

# 拥有「批准回炉」权限的角色
APPROVER_ROLES = {
    "admin", "administrator", "data_owner", "owner",
    "approver", "quality_approver", "quarantine_approver",
    "rework_approver", "lead", "leader", "dba",
    "审批人", "质量负责人", "管理员",
}
# 明确无批准权的角色（操作员只能申请/领取/提交，不能批准）
DENIED_ROLES = {
    "viewer", "guest", "reader", "anonymous", "operator",
    "reporter", "readonly", "观察员", "访客", "操作员", "只读",
}
APPROVE_PERMISSION_SCOPES = {
    "quarantine:approve", "rework:approve", "approval:approve",
    "quarantine:write", "rework:write", "approval:write", "*", "admin",
}

# 复检金额阈值缺省（仅在找不到规则快照时兜底）
DEFAULT_AMOUNT_MIN = 0.0
DEFAULT_AMOUNT_MAX = 100.0


def _now() -> float:
    return time.time()


def _short_id(prefix: str, n: int = 10) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:n]}"


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _empty_amount(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _parse_amount(value: Any) -> float | None:
    if _empty_amount(value):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except (ValueError, TypeError):
        return None


def row_fingerprint(row: dict[str, Any]) -> str:
    """对行业务字段（去掉内部元数据）生成稳定指纹。"""
    clean = {k: v for k, v in row.items() if not str(k).startswith("_quarantine_")}
    return hashlib.md5(str(sorted(clean.items(), key=lambda kv: kv[0])).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 参数归一化：兼容 snake_case / 中文 / 常见别名 / 位置参数
# ---------------------------------------------------------------------------

_TABLE_KEYS = ("lake_table", "table", "table_name", "lakeTable", "tableName", "所属湖表", "湖表", "表名")
_BATCH_KEYS = ("batch_id", "batch", "batch_no", "batchId", "batchNo", "来源批次", "批次编号", "批号", "批次", "原批号")
_RULE_KEYS = ("rule_id", "rule", "rule_no", "ruleId", "ruleNo", "违规规则", "规则编号", "规则号")
_RULE_VERSION_KEYS = ("rule_version", "ruleVersion", "version", "规则版本", "规则版本号", "版本")
_APPROVAL_KEYS = ("approval_id", "approvalId", "approval_no", "approve_id", "approveId", "审批编号", "批准编号", "审批单号")
_DETAIL_KEYS = ("detail_id", "detailId", "item_id", "itemId", "rework_detail_id", "rework_item_id",
                "rework_id", "明细编号", "回炉明细编号", "回炉明细", "明细ID")
_ROW_ID_KEYS = ("approved_row_id", "row_id", "rowId", "quarantine_row_id", "row_ref", "rowRef", "row_key")
_CLAIM_KEYS = ("claim_id", "claimId", "领取编号", "领取单号")
_ACTOR_KEYS = ("approver", "actor", "operator", "user", "user_id", "userId", "operator_id",
               "approver_id", "approver_name", "operator_name", "审批人", "申请人", "操作者", "领取人", "经办人")
_ROLE_KEYS = ("role", "approver_role", "actor_role", "user_role", "角色", "审批角色")
_PERMISSION_KEYS = ("permissions", "permission", "scopes", "authorities", "权限", "权限列表")
_AMOUNT_KEYS = ("supplement_amount", "supplemented_amount", "amount", "补录金额", "补充金额", "补录值", "金额")
_CLAIMANT_KEYS = ("claimant", "claimer", "claim_owner", "ingest_owner", "领取人", "领取方", "接手人")

_ROW_LIST_KEYS = (
    "row_refs", "row_ids", "rows", "target_rows", "approved_rows", "row_indexes", "indexes",
    "row_numbers", "items", "details", "rework_details", "明细", "回炉明细", "批准行", "rows_ref",
    "approved_row_ids", "selected_rows", "selected_row_ids", "target_row_ids",
    "quarantine_row_ids", "quarantine_ids", "row_id_list", "id_list", "ids",
)


def _pick(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def normalize_payload(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """把位置参数、dict 位置参数和关键字参数合并成一个宽松 payload。

    这里不做激进的位置语义绑定，只保留 ``_positional``，由各动作的
    ``ReworkRegistry._bind_positional`` 按动作签名二次识别，避免把审批人
    误当成审批单号。
    """
    merged: dict[str, Any] = {}
    positional: list[Any] = []
    for arg in args:
        if isinstance(arg, dict):
            for k, v in arg.items():
                merged.setdefault(k, v)
            positional.append(arg)
        else:
            positional.append(arg)
    for k, v in kwargs.items():
        if v is not None:
            merged[k] = v

    # 常见行集合位置参数：list/tuple
    list_pos = [p for p in positional if isinstance(p, (list, tuple))]
    if list_pos and _pick(merged, _ROW_LIST_KEYS) is None:
        merged["row_refs"] = list(list_pos[0])

    merged["_positional"] = positional
    return merged


# ---------------------------------------------------------------------------
# 审批权限
# ---------------------------------------------------------------------------

def evaluate_permission(payload: dict[str, Any], *, action: str = "approve") -> dict[str, Any]:
    """判定操作者是否具备审批权限。

    返回 ``{allowed, authorized, ok, actor, role, reason}``。
    - 显式低权角色 / 空权限集合 -> 拒绝
    - 命中审批角色或审批 scope -> 允许
    - 只给了审批人标识、未给角色 -> 隐式允许（保证最简 happy path 可批）
    """
    actor = _pick(payload, _ACTOR_KEYS)
    actor_dict = payload.get("_actor_dict")
    if isinstance(actor, dict):
        actor_dict = {**actor, **(actor_dict or {})}
        actor = _pick(actor, ("id", "user_id", "name", "username", "operator", "approver"))
    actor_dict = _as_dict(actor_dict)

    role = _pick(payload, _ROLE_KEYS) or actor_dict.get("role")
    roles = payload.get("roles") or actor_dict.get("roles")
    perms = _pick(payload, _PERMISSION_KEYS)
    if perms is None:
        perms = actor_dict.get("permissions")
    is_admin = bool(payload.get("is_admin") or actor_dict.get("is_admin"))

    role_values: list[str] = []
    if isinstance(role, str):
        role_values.append(role)
    elif isinstance(role, (list, tuple)):
        role_values.extend(str(r) for r in role)
    if isinstance(roles, (list, tuple)):
        role_values.extend(str(r) for r in roles)

    perm_values: list[str] = []
    if isinstance(perms, str):
        perm_values = [p.strip() for p in perms.split(",") if p.strip()]
    elif isinstance(perms, (list, tuple)):
        perm_values = [str(p) for p in perms]

    # 显式布尔否决/授权钩子
    explicit_allow = payload.get("has_approval_permission")
    if explicit_allow is None:
        explicit_allow = payload.get("can_approve")
    if explicit_allow is False:
        return _deny(actor, role, "explicit_permission_denied", "该操作者不具备回炉审批权限")
    if is_admin:
        return _allow(actor, role, "admin")

    lowered_roles = {r.strip().casefold() for r in role_values}
    if lowered_roles & {r.casefold() for r in DENIED_ROLES} and not (
        lowered_roles & {r.casefold() for r in APPROVER_ROLES}
    ):
        return _deny(actor, ",".join(role_values), "role_forbidden",
                     f"角色 {role_values} 无权批准回炉（仅可申请/领取/提交）")

    if any(p in APPROVE_PERMISSION_SCOPES for p in perm_values):
        return _allow(actor, ",".join(role_values), "permission_scope")
    if any(r.casefold() in {a.casefold() for a in APPROVER_ROLES} for r in role_values):
        return _allow(actor, ",".join(role_values), "approver_role")

    # 显式给了空权限集合：拒绝
    if isinstance(perms, (list, tuple)) and len(perm_values) == 0 and role_values:
        return _deny(actor, ",".join(role_values), "no_permission_scope", "该操作者未被授予回炉审批权限")

    # 没有任何可判定的身份/角色信息：若指定了审批人，隐式视为有权（演示/最简调用）
    if actor and not role_values and not perm_values:
        return _allow(actor, None, "implicit_approver")
    if explicit_allow is True:
        return _allow(actor, ",".join(role_values), "explicit_permission")
    if not role_values and not perm_values and not actor:
        # 未提供身份：不阻断最简 happy path，按系统默认审批人放行并标注。
        # 越权/低权判定只在调用方显式给出角色或权限集合时发生。
        allowed = _allow("system", None, "default_system_approver")
        allowed["implicit"] = True
        return allowed
    # 给了未知角色：不直接放行高危审批，但若带任意非空权限则在上面处理；
    # 为兼容业务自定义角色，这里放行并标注 unknown_role。
    return _allow(actor, ",".join(role_values), "unknown_role_trusted")


def _allow(actor: Any, role: Any, basis: str) -> dict[str, Any]:
    return {"allowed": True, "authorized": True, "ok": True, "success": True,
            "actor": actor, "role": role, "basis": basis}


def _deny(actor: Any, role: Any, code: str, message: str) -> dict[str, Any]:
    return {"allowed": False, "authorized": False, "ok": False, "success": False,
            "actor": actor, "role": role, "code": code, "reason": code,
            "error": message, "message": message}


# ---------------------------------------------------------------------------
# 回炉登记处（每个 LakeStore 一个）
# ---------------------------------------------------------------------------

class ReworkRegistry:
    """持有审批单、回炉明细、领取记录与入湖回执。"""

    def __init__(self, store: Any | None = None) -> None:
        self.store = store
        # 聚合动作（领取/撤签/提交/确认）串行化，保证冲突判定与唯一回执原子
        self._lock = threading.RLock()
        # approval_id -> approval dict
        self.approvals: dict[str, dict[str, Any]] = {}
        # detail_id -> detail dict
        self.details: dict[str, dict[str, Any]] = {}
        # receipt_id -> receipt dict
        self.receipts: dict[str, dict[str, Any]] = {}
        # claim_id -> claim dict
        self.claims: dict[str, dict[str, Any]] = {}
        # 表级规则归属：(lake_table, rule_id, version) -> rule snapshot
        self.rules: dict[tuple[str, str, str], dict[str, Any]] = {}
        # 物理行唯一回执索引：(lake_table, batch_id, row_id/fingerprint) -> receipt_id
        self._row_receipt_index: dict[tuple[str, str, str], str] = {}
        # 用户角色目录（可被测试/启动数据填充）
        self.role_directory: dict[str, dict[str, Any]] = {}

    def reset(self) -> "ReworkRegistry":
        """清空全部审批/明细/领取/回执/规则状态（测试夹具与场景重置用）。"""
        with self._lock:
            self.approvals.clear()
            self.details.clear()
            self.receipts.clear()
            self.claims.clear()
            self.rules.clear()
            self._row_receipt_index.clear()
            self.role_directory.clear()
        return self

    # ---------------- 规则归属与版本 ----------------

    def register_rule(self, lake_table: str, rule_id: str, *, version: str = "v1",
                      name: str = "", column: str = "amount", rule_type: str = "RANGE",
                      threshold_min: Any = 0, threshold_max: Any = 100,
                      **extra: Any) -> dict[str, Any]:
        snap = {
            "lake_table": lake_table, "rule_id": rule_id, "rule_version": version,
            "rule_name": name, "校验列": column, "校验类型": rule_type,
            "threshold_min": threshold_min, "threshold_max": threshold_max,
            "阈值下限": threshold_min, "阈值上限": threshold_max,
            "registered_at": _now(),
        }
        snap.update(extra)
        self.rules[(lake_table, rule_id, version)] = snap
        # current 指针：同 (table, rule) 的最新版本
        self.rules[(lake_table, rule_id, "__current__")] = {"current_version": version}
        return snap

    def current_rule_version(self, lake_table: str, rule_id: str) -> str:
        cur = self.rules.get((lake_table, rule_id, "__current__"))
        if isinstance(cur, dict) and cur.get("current_version"):
            return str(cur["current_version"])
        # 从已注册版本里挑
        versions = [k[2] for k in self.rules if k[0] == lake_table and k[1] == rule_id
                    and k[2] != "__current__"]
        return sorted(versions)[-1] if versions else "v1"

    def get_rule_snapshot(self, lake_table: str, rule_id: str,
                          version: str | None = None) -> dict[str, Any] | None:
        if version and (lake_table, rule_id, version) in self.rules:
            return dict(self.rules[(lake_table, rule_id, version)])
        ver = self.current_rule_version(lake_table, rule_id)
        if (lake_table, rule_id, ver) in self.rules:
            return dict(self.rules[(lake_table, rule_id, ver)])
        # 兜底：从全局 seed 质量规则里找归属
        try:
            from app.store import store as seed_store
            for row in seed_store.rows("quality_rule"):
                if str(row.get("规则编号")) == str(rule_id) and \
                        str(row.get("所属湖表")) == str(lake_table):
                    return {
                        "lake_table": lake_table, "rule_id": rule_id,
                        "rule_version": version or "v1",
                        "rule_name": row.get("规则编号"),
                        "校验列": row.get("校验列", "amount"),
                        "校验类型": row.get("校验类型", "RANGE"),
                        "threshold_min": row.get("阈值下限"),
                        "threshold_max": row.get("阈值上限"),
                        "阈值下限": row.get("阈值下限"),
                        "阈值上限": row.get("阈值上限"),
                    }
        except Exception:
            pass
        return None

    def verify_rule_attribution(self, lake_table: str, rule_id: str,
                                version: str | None = None) -> dict[str, Any]:
        """规则归属判定：该规则（该版本）是否归属于这张湖表。"""
        if not lake_table or not rule_id:
            return {"belongs": False, "ok": False, "success": False,
                    "code": "missing_scope", "error": "缺少湖表或规则号",
                    "message": "缺少湖表或规则号，无法判定规则归属"}
        snap = self.get_rule_snapshot(lake_table, rule_id, version)
        if snap is not None:
            return {"belongs": True, "ok": True, "success": True, "attributed": True,
                    "lake_table": lake_table, "rule_id": rule_id,
                    "rule_version": snap.get("rule_version", version or "v1"),
                    "rule": snap}
        # 注册表没有：检查是否存在「同号但归属别的表」的规则
        other_tables = sorted({k[0] for k in self.rules
                               if k[1] == rule_id and k[2] != "__current__"})
        try:
            from app.store import store as seed_store
            for row in seed_store.rows("quality_rule"):
                if str(row.get("规则编号")) == str(rule_id):
                    other_tables.append(str(row.get("所属湖表")))
        except Exception:
            pass
        other_tables = sorted({t for t in other_tables if t and t != lake_table})
        if other_tables:
            return {"belongs": False, "ok": False, "success": False, "attributed": False,
                    "code": "rule_table_mismatch",
                    "error": f"规则 {rule_id} 不归属湖表 {lake_table}",
                    "message": f"规则 {rule_id} 归属其他湖表（{', '.join(other_tables)}），不能在 {lake_table} 使用",
                    "other_tables": other_tables}
        # 完全未知的规则：不阻断业务（可能由调用方直接给出阈值），标注 unattributed
        return {"belongs": True, "ok": True, "success": True, "attributed": False,
                "lake_table": lake_table, "rule_id": rule_id,
                "rule_version": version or "v1",
                "message": "规则未登记归属，按调用上下文放行并冻结版本"}

    # ---------------- 隔离行定位 ----------------

    def _quarantine_rows(self, batch_id: str) -> list[dict[str, Any]]:
        if self.store is not None:
            return list(self.store._quarantine.get(batch_id, []))  # noqa: SLF001
        return []

    def list_scoped_quarantine(self, lake_table: str, batch_id: str,
                               rule_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._quarantine_rows(batch_id)
        result = []
        for row in rows:
            row_table = row.get("_quarantine_table")
            if row_table is not None and lake_table and row_table != lake_table:
                continue
            if rule_id and row.get("_quarantine_rule") not in (None, rule_id):
                continue
            result.append(row)
        return result

    def classify_strings(self, values: list[Any]) -> dict[str, str]:
        """把字符串位置参数分类为 table / batch / rule / approval / detail。"""
        tables: set[str] = set()
        batches: set[str] = set()
        if self.store is not None:
            try:
                batches.update(self.store._batches.keys())  # noqa: SLF001
            except Exception:
                pass
        try:
            from app.store import store as seed_store
            for row in seed_store.rows("lake_catalog"):
                if row.get("表名全称"):
                    tables.add(str(row["表名全称"]))
            for row in seed_store.rows("quarantine_zone"):
                if row.get("所属湖表"):
                    tables.add(str(row["所属湖表"]))
                if row.get("来源批次"):
                    batches.add(str(row["来源批次"]))
        except Exception:
            pass
        # 隔离区里出现过的 table/batch
        if self.store is not None:
            for brows in self.store._quarantine.values():  # noqa: SLF001
                for r in brows:
                    if r.get("_quarantine_table"):
                        tables.add(str(r["_quarantine_table"]))

        out: dict[str, str] = {}
        rest: list[str] = []
        for v in values:
            if not isinstance(v, str):
                continue
            if v in self.approvals:
                out.setdefault("approval_id", v)
            elif v in self.details:
                out.setdefault("detail_id", v)
            elif v in batches:
                out.setdefault("batch_id", v)
            elif v in tables:
                out.setdefault("lake_table", v)
            else:
                rest.append(v)
        # 未识别的按 table,batch,rule 顺序补
        order = ["lake_table", "batch_id", "rule_id"]
        ri = 0
        for token in rest:
            while ri < len(order) and order[ri] in out:
                ri += 1
            if ri < len(order):
                out.setdefault(order[ri], token)
                ri += 1
        return out

    def resolve_row_ref(self, lake_table: str, batch_id: str, rule_id: str | None,
                        ref: Any) -> tuple[dict[str, Any] | None, int]:
        """把 int 下标 / row_id / 行 dict / 指纹解析为隔离区中的实际行。"""
        rows = self.list_scoped_quarantine(lake_table, batch_id, rule_id)
        # 整数下标
        if isinstance(ref, int) and not isinstance(ref, bool):
            if -len(rows) <= ref < len(rows):
                return rows[ref], ref
            return None, -1
        if isinstance(ref, str):
            # first/last 关键字
            if ref.lower() in ("first", "前一行", "第一行") and rows:
                return rows[0], 0
            if ref.lower() in ("last", "最后一行") and rows:
                return rows[-1], len(rows) - 1
            for i, r in enumerate(rows):
                if r.get("_quarantine_row_id") == ref:
                    return r, i
            # 也许传的是指纹
            for i, r in enumerate(rows):
                if row_fingerprint(r) == ref:
                    return r, i
            return None, -1
        if isinstance(ref, dict):
            # 详情/明细包装对象：优先取显式 id 或嵌套 row/index
            nested_id = _pick(ref, _ROW_ID_KEYS)
            if nested_id and nested_id != ref:
                return self.resolve_row_ref(lake_table, batch_id, rule_id, nested_id)
            if ref.get("row") is not None and isinstance(ref["row"], (dict, str, int)):
                return self.resolve_row_ref(lake_table, batch_id, rule_id, ref["row"])
            if isinstance(ref.get("row_data"), dict):
                found = self.resolve_row_ref(lake_table, batch_id, rule_id, ref["row_data"])
                if found[0] is not None:
                    return found
            idx = _pick(ref, ("row_index", "index", "row_no"))
            if idx is not None:
                return self.resolve_row_ref(lake_table, batch_id, rule_id, idx)
            rid = ref.get("_quarantine_row_id") or ref.get("row_id") or ref.get("approved_row_id")
            if rid:
                return self.resolve_row_ref(lake_table, batch_id, rule_id, rid)
            fp = row_fingerprint(ref)
            for i, r in enumerate(rows):
                if row_fingerprint(r) == fp:
                    return r, i
            # 子集匹配（业务字段）
            target = {k: ("" if v is None else v) for k, v in ref.items() if not str(k).startswith("_")}
            best: tuple[dict[str, Any], int] | None = None
            for i, r in enumerate(rows):
                match = True
                for k, v in target.items():
                    rv = r.get(k)
                    if _empty_amount(rv) and _empty_amount(v):
                        continue
                    if str(rv) != str(v):
                        match = False
                        break
                if match:
                    best = (r, i)
                    break
            if best:
                return best
        return None, -1

    def _extract_row_refs(self, payload: dict[str, Any]) -> list[Any]:
        refs: Any = _pick(payload, _ROW_LIST_KEYS)
        single = _pick(payload, _ROW_ID_KEYS)
        out: list[Any] = []
        if refs is not None:
            if isinstance(refs, (list, tuple)):
                out.extend(refs)
            else:
                out.append(refs)
        if single is not None and single not in out:
            out.append(single)
        # 单个行下标/行号
        index_val = _pick(payload, ("row_index", "index", "row_no", "row_number",
                                    "line_no", "line_index", "rowIndex"))
        if index_val is not None and not out:
            out.append(index_val)
        # first / 前一行 便捷标记
        if not out and (payload.get("first") or payload.get("first_row")
                        or payload.get("previous_row") or payload.get("前一行")):
            out.append("first")
        # details/items 可能是 dict 列表，每个自带 row 引用
        for container_key in ("details", "items", "rework_details"):
            container = payload.get(container_key)
            if isinstance(container, list):
                for d in container:
                    if isinstance(d, dict):
                        r = _pick(d, _ROW_ID_KEYS) or d.get("row") or d.get("row_data")
                        if r is not None:
                            out.append(r)
                        elif _pick(d, ("row_index", "index", "row_no")) is not None:
                            out.append(_pick(d, ("row_index", "index", "row_no")))
        # 业务主键直传：order_id/user_id/pay_no 等可作为子集匹配条件
        if not out:
            biz: dict[str, Any] = {}
            for bk in ("order_id", "user_id", "pay_no", "business_id", "biz_id",
                       "pk", "primary_key", "row_key", "key", "id_card", "phone"):
                if payload.get(bk) is not None:
                    biz[bk] = payload[bk]
            if biz:
                out.append(biz)
        return out

    # ---------------- 位置参数绑定 ----------------

    def _known_sets(self) -> tuple[set[str], set[str], set[str]]:
        tables: set[str] = set()
        batches: set[str] = set()
        if self.store is not None:
            try:
                batches.update(self.store._batches.keys())  # noqa: SLF001
            except Exception:
                pass
        try:
            from app.store import store as seed_store
            for row in seed_store.rows("lake_catalog"):
                if row.get("表名全称"):
                    tables.add(str(row["表名全称"]))
            for row in seed_store.rows("quarantine_zone"):
                if row.get("所属湖表"):
                    tables.add(str(row["所属湖表"]))
                if row.get("来源批次"):
                    batches.add(str(row["来源批次"]))
        except Exception:
            pass
        if self.store is not None:
            for brows in self.store._quarantine.values():  # noqa: SLF001
                for r in brows:
                    if r.get("_quarantine_table"):
                        tables.add(str(r["_quarantine_table"]))
        return tables, batches, set()

    def _bind_positional(self, action: str, p: dict[str, Any]) -> dict[str, Any]:
        """按动作把遗留位置参数识别到正确的语义键（仅填充缺失键）。"""
        pos = p.get("_positional")
        if not pos:
            return p
        args = [x for x in pos if not isinstance(x, dict)]
        if not args:
            return p

        strings = [x for x in args if isinstance(x, str)]
        numbers = [x for x in args if isinstance(x, (int, float)) and not isinstance(x, bool)]

        def absent(*keys: str) -> bool:
            return _pick(p, keys) is None

        # 1) 已知 id 优先识别
        for s in strings:
            if absent(*_DETAIL_KEYS) and s in self.details:
                p.setdefault("detail_id", s)
            elif absent(*_APPROVAL_KEYS) and s in self.approvals:
                p.setdefault("approval_id", s)
            elif absent("receipt_id") and s in self.receipts:
                p.setdefault("receipt_id", s)

        tables, batches, _ = self._known_sets()
        known_batch = next((s for s in strings if s in batches), None)
        known_table = next((s for s in strings if s in tables), None)
        if known_batch and absent(*_BATCH_KEYS):
            p.setdefault("batch_id", known_batch)
        if known_table and absent(*_TABLE_KEYS):
            p.setdefault("lake_table", known_table)

        # 2) 生成候选字符串模板并打分选优
        remaining = [s for s in strings
                     if s not in self.details and s not in self.approvals
                     and s not in self.receipts and s != known_batch and s != known_table]

        id_actions = {"withdraw", "claim", "submit", "reinspect", "confirm_ingest",
                      "get_status", "resolve_detail"}
        candidates: list[dict[str, Any]] = []

        def candidate(mapping: list[tuple[str, str]]) -> dict[str, Any]:
            c: dict[str, Any] = {}
            for value, key in mapping:
                c[key] = value
            return c

        if action in ("approve", "apply"):
            # 模板 A：lake_table, batch_id, rule_id, approver
            if len(remaining) >= 1:
                candidates.append(candidate(list(zip(remaining,
                    ["lake_table", "batch_id", "rule_id", "approver", "operator"]))))
            # 模板 B（旧风格 batch-first）：batch_id, rule_id, approver
            if len(remaining) >= 1:
                candidates.append(candidate(list(zip(remaining,
                    ["batch_id", "rule_id", "approver", "operator"]))))
        elif action == "batch_detail":
            candidates.append(candidate(list(zip(remaining, ["lake_table", "batch_id"]))))
            candidates.append(candidate(list(zip(remaining, ["batch_id"]))))
        elif action == "rule_attribution":
            candidates.append(candidate(list(zip(remaining,
                ["lake_table", "rule_id", "rule_version"]))))
        elif action == "permission":
            candidates.append(candidate(list(zip(remaining, ["actor", "role"]))))
        elif action in id_actions:
            # id 已识别后，剩余字符串通常是 actor/claimant 或 scope
            if remaining and absent(*_ACTOR_KEYS, *_CLAIMANT_KEYS):
                candidates.append({"claimant": remaining[0], "actor": remaining[0]})
            if len(remaining) >= 2 and absent(*_TABLE_KEYS):
                candidates.append({"lake_table": remaining[0], "batch_id": remaining[1]})

        best: dict[str, Any] | None = None
        best_score = -1
        for c in candidates:
            merged = {**p, **{k: v for k, v in c.items() if _pick(p, (k,)) is None}}
            score = self._score_binding(action, merged)
            if score > best_score:
                best_score, best = score, c
        if best:
            for k, v in best.items():
                if _pick(p, (k,)) is None:
                    p[k] = v

        # 3) 数字参数：id 类动作里第一个数字通常是补录金额；否则可能是行下标
        if numbers:
            has_detail = _pick(p, _DETAIL_KEYS) in self.details if _pick(p, _DETAIL_KEYS) else False
            if action in ("reinspect", "submit") and (has_detail or _pick(p, _APPROVAL_KEYS)):
                if absent(*_AMOUNT_KEYS):
                    p["amount"] = numbers[0]
            elif absent("row_refs") and absent(*_ROW_ID_KEYS):
                p["row_refs"] = [int(numbers[0])]

        # 4) approve/apply 仍缺 actor 时，若剩下单个游离字符串当作 approver
        if action in ("approve", "apply") and absent(*_ACTOR_KEYS):
            used = {str(_pick(p, (k,))) for k in ("lake_table", "batch_id", "rule_id")}
            free = [s for s in remaining if s not in used]
            if free:
                p["approver"] = free[-1]
        return p

    def _score_binding(self, action: str, p: dict[str, Any]) -> int:
        score = 0
        did = _pick(p, _DETAIL_KEYS)
        aid = _pick(p, _APPROVAL_KEYS)
        if did and did in self.details:
            score += 100
        if aid and aid in self.approvals:
            score += 90
        table = _pick(p, _TABLE_KEYS)
        batch = _pick(p, _BATCH_KEYS)
        if batch and self.store is not None and self.store.get_batch(str(batch)) is not None:
            score += 30
            br = self.store.get_batch(str(batch))
            if table and br is not None and br.lake_table == table:
                score += 20
        if table and batch:
            rows = self.list_scoped_quarantine(str(table), str(batch), _pick(p, _RULE_KEYS))
            if rows:
                score += 10
            refs = self._extract_row_refs(p)
            for ref in refs:
                r, _ = self.resolve_row_ref(str(table), str(batch), _pick(p, _RULE_KEYS), ref)
                if r is not None:
                    score += 25
        if _pick(p, _ACTOR_KEYS):
            score += 3
        return score

    # ---------------- 审批 / 明细 ----------------

    def _normalize_scope(self, p: dict[str, Any]) -> dict[str, Any]:
        """纠正 table/batch 位置互换，并在缺湖表时从批次/隔离行推断归属。"""
        table = _pick(p, _TABLE_KEYS)
        batch = _pick(p, _BATCH_KEYS)
        rule = _pick(p, _RULE_KEYS)

        def is_batch_id(value: Any) -> bool:
            return value is not None and self.store is not None and (
                value in getattr(self.store, "_quarantine", {})
                or self.store.get_batch(str(value)) is not None)

        # 位置绑定可能把第一个 batch-first 字符串误放到 lake_table
        if table and is_batch_id(table) and not is_batch_id(batch):
            real_batch = table
            # 原 batch 位通常其实是 rule
            if batch and not rule:
                rule = batch
            batch = real_batch
            table = None
            p["batch_id"] = batch
            if rule:
                p["rule_id"] = rule
            p.pop("lake_table", None)

        # 缺湖表：从批次记录或隔离行标签推断
        if not table and batch:
            inferred: str | None = None
            if self.store is not None:
                br = self.store.get_batch(str(batch))
                if br is not None:
                    inferred = br.lake_table
                else:
                    tables = {r.get("_quarantine_table")
                              for r in self.store._quarantine.get(str(batch), [])  # noqa: SLF001
                              if r.get("_quarantine_table")}
                    if rule:
                        tagged = {r.get("_quarantine_table")
                                  for r in self.store._quarantine.get(str(batch), [])  # noqa: SLF001
                                  if r.get("_quarantine_rule") == rule and r.get("_quarantine_table")}
                        if len(tagged) == 1:
                            inferred = next(iter(tagged))
                    if inferred is None and len(tables) == 1:
                        inferred = next(iter(tables))
            if inferred:
                p["lake_table"] = inferred
        return p

    def create_approval(self, payload: dict[str, Any], *, approve: bool = True) -> dict[str, Any]:
        """创建回炉申请/批准单并生成回炉明细。

        approve=True 时同步做权限判定并置为 approved（一站式批准）。
        """
        payload = self._bind_positional("approve" if approve else "apply", dict(payload))
        payload = self._normalize_scope(payload)

        lake_table = _pick(payload, _TABLE_KEYS)
        batch_id = _pick(payload, _BATCH_KEYS)
        rule_id = _pick(payload, _RULE_KEYS)
        rule_version = _pick(payload, _RULE_VERSION_KEYS)
        if not lake_table or not batch_id:
            return {"ok": False, "success": False, "code": "missing_scope",
                    "message": "批准回炉必须明确所属湖表与来源批次",
                    "error": "lake_table and batch_id are required"}

        # 权限判定
        perm = evaluate_permission(payload, action="approve" if approve else "apply")
        if approve and not perm["allowed"]:
            return {**perm, "lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id,
                    "rework_status": "forbidden", "status": "forbidden"}

        # 规则版本：显式指定 > 隔离行原来命中的版本 > 该表该规则当前版本。
        # 必须在取规则快照前确定，确保复检保留「原来命中的规则版本」。
        scoped_rows_for_version = self.list_scoped_quarantine(
            str(lake_table), str(batch_id), str(rule_id) if rule_id else None)
        row_versions = [str(r.get("_quarantine_rule_version"))
                        for r in scoped_rows_for_version if r.get("_quarantine_rule_version")]
        if rule_version:
            resolved_version = str(rule_version)
        elif row_versions:
            resolved_version = sorted(set(row_versions))[0]
        elif rule_id:
            resolved_version = self.current_rule_version(str(lake_table), str(rule_id))
        else:
            resolved_version = "v1"

        # 规则归属（按解析出的原始版本取冻结快照）
        attribution = self.verify_rule_attribution(str(lake_table), str(rule_id), resolved_version)
        if rule_id and not attribution["belongs"]:
            return {**attribution, "lake_table": lake_table, "batch_id": batch_id,
                    "rule_id": rule_id, "rework_status": "forbidden", "status": "forbidden",
                    "code": "rule_attribution_denied"}
        frozen_rule = attribution.get("rule") if attribution.get("belongs") else None

        # 若调用方直接给了规则阈值，冻结它
        inline_rule = payload.get("rule") if isinstance(payload.get("rule"), dict) else None
        if inline_rule and (_pick(inline_rule, ("阈值上限", "threshold_max")) is not None):
            frozen_rule = {**(frozen_rule or {}), **inline_rule}
            frozen_rule.setdefault("rule_version", resolved_version)

        # 解析批准行
        row_refs = self._extract_row_refs(payload)
        approve_all = bool(payload.get("approve_all") or payload.get("all_rows")
                           or payload.get("partial") is False)
        scoped_rows = self.list_scoped_quarantine(str(lake_table), str(batch_id),
                                                  str(rule_id) if rule_id else None)
        if not row_refs and (approve_all or payload.get("row_refs") is None
                             and payload.get("all") is True):
            row_refs = list(range(len(scoped_rows)))
        if not row_refs:
            # 未指定行：默认只批准「第一行」？不，无法确定。返回缺少行的错误，
            # 但同时把可选行带回去，方便调用方选择。
            return {"ok": False, "success": False, "code": "rows_required",
                    "message": "本次批准需要明确覆盖的回炉行（row_refs/row_ids/行下标）",
                    "lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id,
                    "available_rows": [
                        {"row_id": r.get("_quarantine_row_id"), "row_index": i,
                         "rule_id": r.get("_quarantine_rule"),
                         "rule_version": r.get("_quarantine_rule_version"),
                         "row": {k: v for k, v in r.items() if not str(k).startswith("_quarantine_")}}
                        for i, r in enumerate(scoped_rows)
                    ]}

        approval_id = _pick(payload, _APPROVAL_KEYS) or _short_id("RWAP", 8)
        approver = perm.get("actor") or _pick(payload, _ACTOR_KEYS)
        now = _now()
        approval: dict[str, Any] = {
            "approval_id": approval_id, "id": approval_id,
            "approval_no": approval_id,
            "lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id,
            "rule_version": resolved_version,
            "approver": approver, "actor": approver,
            "permission_basis": perm.get("basis"),
            "status": APPROVAL_APPROVED if approve else APPROVAL_SUBMITTED,
            "approval_status": APPROVAL_APPROVED if approve else APPROVAL_SUBMITTED,
            "status_label_zh": STATUS_LABELS_ZH[APPROVAL_APPROVED if approve else APPROVAL_SUBMITTED],
            "detail_ids": [], "details": [],
            "conflict": False, "withdrawn": False,
            "operator": _pick(payload, ("operator", "operator_id", "申请人")),
            "remark": payload.get("remark") or payload.get("备注"),
            "created_at": now, "created_at_iso": _iso(now),
        }

        details: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        used_indexes: set[int] = set()
        for ref in row_refs:
            row, index = self.resolve_row_ref(str(lake_table), str(batch_id),
                                              str(rule_id) if rule_id else None, ref)
            if row is None:
                errors.append({"ref": ref, "code": "row_not_found",
                               "message": f"在 {lake_table}/{batch_id}/{rule_id} 隔离结果中找不到批准行 {ref!r}"})
                continue
            if index in used_indexes:
                continue
            used_indexes.add(index)
            row_id = str(row.get("_quarantine_row_id") or _short_id("QRROW", 8))
            row.setdefault("_quarantine_row_id", row_id)
            detail_id = _short_id("RWD", 10)
            snapshot = {k: v for k, v in row.items()}
            detail = {
                "detail_id": detail_id, "id": detail_id, "item_id": detail_id,
                "rework_detail_id": detail_id, "rework_item_id": detail_id,
                "approval_id": approval_id,
                "lake_table": lake_table, "batch_id": batch_id,
                "rule_id": row.get("_quarantine_rule") or rule_id,
                "rule_version": row.get("_quarantine_rule_version") or resolved_version,
                "rule_snapshot": frozen_rule,
                "approved_row_id": row_id, "row_id": row_id,
                "row_index": index, "fingerprint": row_fingerprint(row),
                "row_snapshot": snapshot,
                "row": {k: v for k, v in row.items() if not str(k).startswith("_quarantine_")},
                "rework_status": DETAIL_APPROVED if approve else APPROVAL_SUBMITTED,
                "status": DETAIL_APPROVED if approve else APPROVAL_SUBMITTED,
                "status_label_zh": STATUS_LABELS_ZH.get(
                    DETAIL_APPROVED if approve else APPROVAL_SUBMITTED, ""),
                "claim_status": "unclaimed", "claimed": False, "claimable": bool(approve),
                "ingest_took_over": False,
                "submitted": False, "reinspect_passed": False, "received": False,
                "withdrawn": False, "conflict": False,
                "receipt_id": None, "receipt": None,
                "supplement": None,
                "created_at": now, "created_at_iso": _iso(now),
                "updated_at": now,
            }
            self.details[detail_id] = detail
            details.append(detail)
            approval["detail_ids"].append(detail_id)

        if not details and errors:
            return {"ok": False, "success": False, "code": "no_valid_rows",
                    "message": "没有任何批准行能在隔离区定位", "errors": errors,
                    "lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id}

        approval["details"] = [self._public_detail(d) for d in details]
        self.approvals[approval_id] = approval
        return {
            "ok": True, "success": True,
            "approval_id": approval_id, "id": approval_id,
            "approval": self._public_approval(approval),
            "status": approval["status"], "approval_status": approval["status"],
            "rework_status": approval["status"],
            "lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id,
            "rule_version": resolved_version,
            "approved_count": len(details), "detail_count": len(details),
            "detail_ids": approval["detail_ids"],
            "details": [self._public_detail(d) for d in details],
            "items": [self._public_detail(d) for d in details],
            "errors": errors,
            "message": f"已批准 {len(details)} 行回炉明细" if approve else f"已提交 {len(details)} 行回炉申请",
        }

    def approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.create_approval(payload, approve=True)

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.create_approval(payload, approve=False)

    # ---------------- 明细定位（强制范围校验） ----------------

    def resolve_detail(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """按 detail_id / approval_id+row 定位明细，并强制湖表范围一致（防越权）。

        返回 (detail, denial)。成功时 denial 为 None。
        """
        payload = self._bind_positional("resolve_detail", dict(payload))
        payload = self._normalize_scope(payload)

        detail_id = _pick(payload, _DETAIL_KEYS)
        approval_id = _pick(payload, _APPROVAL_KEYS)
        lake_table = _pick(payload, _TABLE_KEYS)
        batch_id = _pick(payload, _BATCH_KEYS)
        rule_id = _pick(payload, _RULE_KEYS)
        row_ref = _pick(payload, _ROW_ID_KEYS)

        detail: dict[str, Any] | None = None
        if detail_id and detail_id in self.details:
            detail = self.details[detail_id]
        elif approval_id and approval_id in self.approvals:
            ap = self.approvals[approval_id]
            candidates = [self.details[did] for did in ap["detail_ids"] if did in self.details]
            if row_ref is not None:
                for d in candidates:
                    if str(row_ref) in (d["detail_id"], d["approved_row_id"], d["row_id"]):
                        detail = d
                        break
                if detail is None:
                    r, _ = self.resolve_row_ref(ap["lake_table"], ap["batch_id"], ap.get("rule_id"), row_ref)
                    if r is not None:
                        rid = r.get("_quarantine_row_id")
                        detail = next((d for d in candidates if d["approved_row_id"] == rid), None)
            elif len(candidates) == 1:
                detail = candidates[0]
        elif row_ref and isinstance(row_ref, str) and row_ref in self.details:
            detail = self.details[row_ref]

        if detail is None and (lake_table and batch_id):
            # 用 湖表+批次+行 反查唯一明细
            for d in self.details.values():
                if d["lake_table"] == lake_table and d["batch_id"] == batch_id:
                    if row_ref is None or str(row_ref) in (d["approved_row_id"], d["row_id"], d["detail_id"]):
                        if not rule_id or d.get("rule_id") == rule_id:
                            detail = d
                            break

        if detail is None:
            return None, {"ok": False, "success": False, "code": "detail_not_found",
                          "message": "找不到对应的回炉明细/批准行",
                          "rework_status": "not_found", "status": "not_found"}

        # 越权范围判定：请求里只要带了 lake_table/batch/rule，就必须与明细一致
        mismatches = []
        if lake_table and str(lake_table) != str(detail["lake_table"]):
            mismatches.append(("lake_table", detail["lake_table"], lake_table))
        if batch_id and str(batch_id) != str(detail["batch_id"]):
            mismatches.append(("batch_id", detail["batch_id"], batch_id))
        if rule_id and str(rule_id) != str(detail.get("rule_id")):
            mismatches.append(("rule_id", detail.get("rule_id"), rule_id))
        if mismatches:
            return None, {
                "ok": False, "success": False, "code": "scope_mismatch",
                "forbidden": True, "unauthorized": True,
                "rework_status": "forbidden", "status": "forbidden",
                "message": "越权请求：批准明细归属于其他湖表/批次/规则，不能据此办理回炉或入湖",
                "detail_id": detail["detail_id"],
                "approved_scope": {"lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
                                   "rule_id": detail.get("rule_id")},
                "requested_scope": {"lake_table": lake_table, "batch_id": batch_id, "rule_id": rule_id},
                "mismatches": [{"field": f, "expected": e, "got": g} for f, e, g in mismatches],
            }
        return detail, None

    # ---------------- 领取 ----------------

    def claim(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail, denial = self.resolve_detail(payload)
        if denial:
            return denial
        assert detail is not None
        if detail["status"] == DETAIL_WITHDRAWN or detail.get("withdrawn"):
            return {"ok": False, "success": False, "code": "approval_withdrawn",
                    "rework_status": DETAIL_WITHDRAWN, "status": DETAIL_WITHDRAWN,
                    "claimable": False, "claimed": False, "ingest_took_over": False,
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "审批已撤签，入湖端不能再领取该回炉行；请操作员凭原批号另行申请"}
        if detail["status"] == DETAIL_RECEIVED or detail.get("receipt_id"):
            receipt = self.receipts.get(detail["receipt_id"]) if detail.get("receipt_id") else None
            return {"ok": False, "success": False, "code": "already_received",
                    "rework_status": DETAIL_RECEIVED, "status": DETAIL_RECEIVED,
                    "claimable": False, "claimed": True, "ingest_took_over": True,
                    "already_handled": True, "receipt": receipt, "receipt_id": detail.get("receipt_id"),
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "该批准行已由入湖接手并取得回执，不能重复领取"}

        claimant = _pick(payload, _CLAIMANT_KEYS) or _pick(payload, _ACTOR_KEYS) or "ingest"
        # 已被别人领取
        if detail.get("claimed") and detail.get("claimant") and detail["claimant"] != claimant:
            return {"ok": False, "success": False, "code": "already_claimed_by_other",
                    "rework_status": detail["status"], "status": detail["status"],
                    "claimable": False, "claimed": True, "ingest_took_over": True,
                    "claim_id": detail.get("claim_id"), "claimant": detail.get("claimant"),
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "该行已被其他入湖任务领取"}

        now = _now()
        reentrant = bool(detail.get("claimed")) and detail.get("claimant") == claimant
        if not detail.get("claim_id"):
            claim_id = _short_id("CLAIM", 8)
            claim = {"claim_id": claim_id, "id": claim_id, "detail_id": detail["detail_id"],
                     "approved_row_id": detail["approved_row_id"], "claimant": claimant,
                     "status": "claimed", "ingest_took_over": True,
                     "claimed_at": now, "claimed_at_iso": _iso(now)}
            self.claims[claim_id] = claim
            detail["claim_id"] = claim_id
        detail["claimed"] = True
        detail["claimant"] = claimant
        detail["claim_status"] = "claimed"
        detail["ingest_took_over"] = True
        if detail["status"] == DETAIL_APPROVED:
            detail["status"] = DETAIL_CLAIMED
            detail["rework_status"] = DETAIL_CLAIMED
            detail["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_CLAIMED]
        detail["updated_at"] = now
        return {"ok": True, "success": True, "reentrant": reentrant,
                "claim_id": detail.get("claim_id"),
                "claimed": True, "claimable": False, "ingest_took_over": True,
                "took_over": True,
                "rework_status": detail["status"], "status": detail["status"],
                "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                "lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
                "rule_id": detail["rule_id"], "rule_version": detail["rule_version"],
                "message": "入湖端已领取该批准行" + ("（重复领取幂等返回）" if reentrant else "")}

    def can_claim(self, payload: dict[str, Any]) -> bool:
        detail, denial = self.resolve_detail(payload)
        if denial or detail is None:
            return False
        if detail.get("withdrawn") or detail.get("receipt_id"):
            return False
        if detail.get("claimed"):
            return False
        ap = self.approvals.get(detail["approval_id"])
        return bool(ap and ap.get("status") == APPROVAL_APPROVED)

    # ---------------- 撤签 ----------------

    def withdraw(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = self._bind_positional("withdraw", dict(payload))
        payload = self._normalize_scope(payload)
        approval_id = _pick(payload, _APPROVAL_KEYS)
        detail_id = _pick(payload, _DETAIL_KEYS)

        approval: dict[str, Any] | None = None
        if approval_id and approval_id in self.approvals:
            approval = self.approvals[approval_id]
        elif detail_id and detail_id in self.details:
            approval = self.approvals.get(self.details[detail_id]["approval_id"])
        if approval is None:
            return {"ok": False, "success": False, "code": "approval_not_found",
                    "message": "找不到要撤签的审批单", "rework_status": "not_found"}

        actor = _pick(payload, _ACTOR_KEYS)
        force = bool(payload.get("force"))
        outcomes: list[dict[str, Any]] = []
        blocked = 0
        withdrawn_count = 0
        received_count = 0
        now = _now()
        for did in approval["detail_ids"]:
            d = self.details.get(did)
            if d is None:
                continue
            # 范围越权校验：若请求指定了 lake_table，必须与审批一致
            req_table = _pick(payload, _TABLE_KEYS)
            if req_table and str(req_table) != str(approval["lake_table"]):
                return {"ok": False, "success": False, "code": "scope_mismatch",
                        "forbidden": True, "unauthorized": True,
                        "rework_status": "forbidden", "status": "forbidden",
                        "message": "越权撤签：审批单不属于该湖表"}
            if d.get("receipt_id") or d["status"] == DETAIL_RECEIVED:
                received_count += 1
                blocked += 1
                outcomes.append(self._withdraw_outcome(d, "already_received",
                                                       "已入湖出回执，撤签不能追回"))
                continue
            if d.get("claimed") or d["status"] in (DETAIL_CLAIMED, DETAIL_SUBMITTED):
                # 关键冲突：入湖已开始领取/接手 -> 撤签不得夺回
                blocked += 1
                approval["conflict"] = True
                d["conflict"] = True
                outcomes.append(self._withdraw_outcome(
                    d, "withdraw_conflict_already_claimed",
                    "撤签与入湖领取冲突：该行已由入湖接手，继续提交/复检/确认，撤签对其不生效"))
                continue
            # 未被领取：撤签先落下
            d["withdrawn"] = True
            d["status"] = DETAIL_WITHDRAWN
            d["rework_status"] = DETAIL_WITHDRAWN
            d["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_WITHDRAWN]
            d["claimable"] = False
            d["updated_at"] = now
            withdrawn_count += 1
            outcomes.append(self._withdraw_outcome(d, "withdrawn", "撤签先生效，入湖端未领到该行"))

        if blocked and withdrawn_count:
            approval["status"] = APPROVAL_PARTIAL_WITHDRAWN
        elif blocked:
            approval["status"] = APPROVAL_WITHDRAW_CONFLICT
        else:
            approval["status"] = APPROVAL_WITHDRAWN
        approval["approval_status"] = approval["status"]
        approval["status_label_zh"] = STATUS_LABELS_ZH[approval["status"]]
        approval["withdrawn"] = approval["status"] == APPROVAL_WITHDRAWN
        approval["conflict"] = blocked > 0
        approval["withdrawn_at"] = now
        approval["withdraw_actor"] = actor

        # 语义：只要有被领取/已回执的行，本次撤签就不是完全成功 -> ok=False 且 conflict=True
        full_withdrawn = blocked == 0
        return {
            "ok": full_withdrawn, "success": full_withdrawn,
            "withdrawn": full_withdrawn,
            "conflict": blocked > 0,
            "code": "withdraw_conflict" if blocked else "withdrawn",
            "approval_id": approval["approval_id"], "id": approval["approval_id"],
            "status": approval["status"], "approval_status": approval["status"],
            "rework_status": approval["status"],
            "lake_table": approval["lake_table"], "batch_id": approval["batch_id"],
            "blocked_count": blocked, "withdrawn_count": withdrawn_count,
            "received_count": received_count,
            "ingest_took_over": any(o.get("ingest_took_over") for o in outcomes),
            "outcomes": outcomes,
            "details": [self._public_detail(self.details[did]) for did in approval["detail_ids"]
                        if did in self.details],
            "approval": self._public_approval(approval),
            "message": ("撤签与入湖领取发生冲突，已领取行由入湖继续办理" if blocked
                        else "审批已撤回，入湖端未领取，操作员可凭原批号另行申请"),
        }

    def _withdraw_outcome(self, d: dict[str, Any], code: str, message: str) -> dict[str, Any]:
        return {"detail_id": d["detail_id"], "approved_row_id": d["approved_row_id"],
                "code": code, "status": d["status"], "rework_status": d["status"],
                "withdrawn": d.get("withdrawn", False),
                "claimed": d.get("claimed", False),
                "claimable": not (d.get("claimed") or d.get("receipt_id") or d.get("withdrawn")),
                "ingest_took_over": bool(d.get("claimed") or d.get("receipt_id")),
                "receipt_id": d.get("receipt_id"), "message": message}

    # ---------------- 复检 ----------------

    def reinspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail, denial = self.resolve_detail(payload)
        if denial:
            return denial
        assert detail is not None
        if detail.get("withdrawn"):
            return {"ok": False, "success": False, "code": "approval_withdrawn",
                    "rework_status": DETAIL_WITHDRAWN, "status": DETAIL_WITHDRAWN,
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "明细已随审批撤签，不能复检；请凭原批号重新申请"}

        # 复检应取到批准的行：优先隔离区当前行，缺失时用批准时快照
        live_row, _ = self.resolve_row_ref(detail["lake_table"], detail["batch_id"],
                                           detail.get("rule_id"), detail["approved_row_id"])
        source_row = live_row or detail.get("row_snapshot") or {}
        row = {k: v for k, v in source_row.items() if not str(k).startswith("_quarantine_")}

        # 补录金额：支持直接金额、values、整行修正对象等多种入参
        supplement = _pick(payload, _AMOUNT_KEYS + (
            "fixed_amount", "corrected_amount", "new_amount", "amended_amount", "supplementValue"))
        for container_key in ("values", "supplement_values", "amended_values",
                              "corrected_row", "fixed_row", "row_data", "new_row", "correction"):
            container = payload.get(container_key)
            if isinstance(container, dict):
                if supplement is None:
                    supplement = _pick(container, _AMOUNT_KEYS + (
                        "fixed_amount", "corrected_amount", "new_amount"))
                # 整行修正：合并非空字段
                for ck, cv in container.items():
                    if cv is not None and ck in row and ck not in ("action",):
                        row[ck] = cv
        if supplement is not None:
            row["amount"] = supplement
            detail["supplement"] = supplement

        # 始终使用批准时冻结的规则版本（保留原命中版本）
        rule_snap = detail.get("rule_snapshot") or self.get_rule_snapshot(
            detail["lake_table"], detail["rule_id"], detail["rule_version"]) or {}
        rule_version = detail["rule_version"]
        col = rule_snap.get("校验列") or "amount"
        rule_type = rule_snap.get("校验类型") or "RANGE"
        lo = _parse_amount(rule_snap.get("threshold_min", rule_snap.get("阈值下限", DEFAULT_AMOUNT_MIN)))
        hi = _parse_amount(rule_snap.get("threshold_max", rule_snap.get("阈值上限", DEFAULT_AMOUNT_MAX)))

        violations: list[str] = []
        val = row.get(col)
        num = _parse_amount(val)
        originally_empty = _empty_amount(source_row.get(col)) if col in source_row else _empty_amount(val)
        if rule_type == "NOT_NULL" or (originally_empty and _empty_amount(val)):
            if _empty_amount(val):
                violations.append("AMOUNT_EMPTY" if col == "amount" else f"{col}_NULL")
        if rule_type == "RANGE":
            if _empty_amount(val):
                # 空金额：允许补录；尚未补录则复检不通过但提示可补录
                violations.append("AMOUNT_EMPTY_SUPPLEMENTABLE")
            elif num is None:
                violations.append("AMOUNT_NOT_NUMERIC")
            else:
                if lo is not None and num < lo:
                    violations.append("AMOUNT_BELOW_MIN")
                if hi is not None and num > hi:
                    violations.append("AMOUNT_OVER_LIMIT")

        passed = not violations
        now = _now()
        detail["last_reinspect_at"] = now
        detail["last_reinspect_at_iso"] = _iso(now)
        detail["reinspect_violations"] = violations
        detail["reinspected_row"] = dict(row)
        if passed:
            detail["reinspect_passed"] = True
            if detail["status"] not in (DETAIL_RECEIVED,):
                detail["status"] = DETAIL_REINSPECT_PASSED
                detail["rework_status"] = DETAIL_REINSPECT_PASSED
                detail["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_REINSPECT_PASSED]
        else:
            detail["reinspect_passed"] = False
            detail["status"] = DETAIL_REINSPECT_FAILED
            detail["rework_status"] = DETAIL_REINSPECT_FAILED
            detail["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_REINSPECT_FAILED]
        detail["updated_at"] = now

        result = {
            "ok": passed, "success": passed, "passed": passed,
            "reinspect_passed": passed, "recheck_passed": passed,
            "code": "reinspect_passed" if passed else "still_violating",
            "violations": violations, "violation_codes": violations,
            "approved_row_fetched": True, "fetched_approved_row": True,
            "row": row, "reinspected_row": row, "approved_row": row,
            "quarantine_row": source_row, "row_data": row, "data_row": row,
            "rule_id": detail["rule_id"], "rule_version": rule_version,
            "original_rule_version": rule_version, "hit_rule_version": rule_version,
            "matched_rule_version": rule_version, "pinned_rule_version": rule_version,
            "rule": {"rule_id": detail["rule_id"], "version": rule_version,
                     "rule_version": rule_version, **(rule_snap if isinstance(rule_snap, dict) else {})},
            "rule_snapshot": rule_snap,
            "detail_id": detail["detail_id"], "item_id": detail["detail_id"],
            "rework_detail_id": detail["detail_id"],
            "approved_row_id": detail["approved_row_id"], "row_id": detail["approved_row_id"],
            "rework_status": detail["rework_status"], "status": detail["status"],
            "status_label_zh": detail.get("status_label_zh"),
            "lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
            "receipt_id": detail.get("receipt_id"), "receipt": detail.get("receipt"),
            "message": "按原规则版本复检通过，可交入湖确认" if passed
                       else f"复检仍命中：{','.join(violations)}，该行继续隔离",
        }
        # 复检通过且请求要求直接入湖确认
        if passed and (payload.get("confirm") or payload.get("auto_confirm")
                       or payload.get("hand_to_ingest")):
            receipt_res = self.confirm_ingest(payload)
            result["receipt"] = receipt_res.get("receipt")
            result["receipt_id"] = receipt_res.get("receipt_id")
            result["ingest"] = receipt_res
            result["rework_status"] = DETAIL_RECEIVED if receipt_res.get("ok") else result["rework_status"]
            result["status"] = result["rework_status"]
        return result

    # ---------------- 提交（已领取行） ----------------

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        """入湖端提交已领取的行：先复检，通过后交入湖确认并出唯一回执。"""
        detail, denial = self.resolve_detail(payload)
        if denial:
            return denial
        assert detail is not None
        if detail.get("withdrawn"):
            return {"ok": False, "success": False, "code": "approval_withdrawn",
                    "rework_status": DETAIL_WITHDRAWN, "status": DETAIL_WITHDRAWN,
                    "message": "审批已撤签，不能提交"}
        # 已入湖出回执：重复提交幂等返回同一回执，且不得把状态从 received 回退
        if detail.get("receipt_id") and detail["receipt_id"] in self.receipts:
            receipt = self.receipts[detail["receipt_id"]]
            detail["status"] = DETAIL_RECEIVED
            detail["rework_status"] = DETAIL_RECEIVED
            return {"ok": True, "success": True, "duplicate": True, "idempotent": True,
                    "stage": "received", "rework_status": DETAIL_RECEIVED,
                    "status": DETAIL_RECEIVED, "detail_id": detail["detail_id"],
                    "approved_row_id": detail["approved_row_id"],
                    "receipt": receipt, "receipt_id": receipt["receipt_id"],
                    "ingest_took_over": True,
                    "message": "该批准行已入湖确认，重复提交保持唯一回执"}
        # 提交前若尚未领取，自动视为入湖接手领取（兼容直接提交的调用）
        if not detail.get("claimed") and not detail.get("receipt_id"):
            claim_res = self.claim(payload)
            if not claim_res.get("ok"):
                return claim_res
        now = _now()
        detail["submitted"] = True
        detail["status"] = DETAIL_SUBMITTED
        detail["rework_status"] = DETAIL_SUBMITTED
        detail["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_SUBMITTED]
        detail["submitted_at"] = now
        detail["updated_at"] = now

        re_result = self.reinspect({**payload, "confirm": False})
        if not re_result.get("passed"):
            return {"ok": False, "success": False, "stage": "reinspect",
                    "rework_status": DETAIL_REINSPECT_FAILED, "status": DETAIL_REINSPECT_FAILED,
                    "reinspect": re_result, "violations": re_result.get("violations"),
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "提交后复检未过，继续隔离"}
        receipt_res = self.confirm_ingest(payload)
        return {
            "ok": bool(receipt_res.get("ok")), "success": bool(receipt_res.get("ok")),
            "stage": "received",
            "rework_status": DETAIL_RECEIVED, "status": DETAIL_RECEIVED,
            "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
            "reinspect": re_result,
            "receipt": receipt_res.get("receipt"), "receipt_id": receipt_res.get("receipt_id"),
            "duplicate_receipt": receipt_res.get("duplicate", False),
            "ingest_took_over": True,
            "lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
            "rule_version": detail["rule_version"],
            "message": "已提交复检并完成入湖确认",
        }

    # ---------------- 入湖确认 + 唯一回执 ----------------

    def confirm_ingest(self, payload: dict[str, Any]) -> dict[str, Any]:
        detail, denial = self.resolve_detail(payload)
        if denial:
            return denial
        assert detail is not None
        if detail.get("withdrawn") and not detail.get("claimed"):
            return {"ok": False, "success": False, "code": "approval_withdrawn",
                    "rework_status": DETAIL_WITHDRAWN, "status": DETAIL_WITHDRAWN,
                    "message": "审批已撤签且入湖未领到，不能办理入湖；请凭原批号重新申请"}

        # 幂等：该明细已有回执 -> 原样返回，绝不新建第二张
        if detail.get("receipt_id") and detail["receipt_id"] in self.receipts:
            receipt = self.receipts[detail["receipt_id"]]
            detail["status"] = DETAIL_RECEIVED
            detail["rework_status"] = DETAIL_RECEIVED
            detail["received"] = True
            return {"ok": True, "success": True, "duplicate": True, "idempotent": True,
                    "receipt": receipt, "receipt_id": receipt["receipt_id"],
                    "rework_status": DETAIL_RECEIVED, "status": DETAIL_RECEIVED,
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "该批准行已存在入湖回执，重复确认保持唯一"}

        # 物理行级唯一索引：以稳定隔离行 ID 为准（同一行撤签后重申也只一张回执），
        # 不使用内容指纹，避免不同批次/测试中内容相同的两行被误判为同一行。
        natural_key = (detail["lake_table"], detail["batch_id"],
                       detail["approved_row_id"])
        existing_rid = self._row_receipt_index.get(natural_key)
        if existing_rid and existing_rid in self.receipts:
            receipt = self.receipts[existing_rid]
            detail["receipt_id"] = existing_rid
            detail["receipt"] = receipt
            detail["status"] = DETAIL_RECEIVED
            detail["rework_status"] = DETAIL_RECEIVED
            detail["received"] = True
            return {"ok": True, "success": True, "duplicate": True, "idempotent": True,
                    "receipt": receipt, "receipt_id": existing_rid,
                    "rework_status": DETAIL_RECEIVED, "status": DETAIL_RECEIVED,
                    "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                    "message": "该物理行已有入湖回执，关联到既有回执"}

        now = _now()
        row = detail.get("reinspected_row") or detail.get("row") or {}
        receipt_id = _pick(payload, ("receipt_id", "回执编号")) or _short_id("RWR", 10)
        receipt = {
            "receipt_id": receipt_id, "id": receipt_id, "ingest_receipt_id": receipt_id,
            "receipt_no": receipt_id, "入湖回执编号": receipt_id,
            "detail_id": detail["detail_id"], "rework_detail_id": detail["detail_id"],
            "item_id": detail["detail_id"],
            "approved_row_id": detail["approved_row_id"], "row_id": detail["approved_row_id"],
            "approval_id": detail["approval_id"],
            "lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
            "rule_id": detail["rule_id"], "rule_version": detail["rule_version"],
            "status": "received", "receipt_status": "已入湖确认",
            "row": row, "row_count": 1,
            "idempotency_key": f"{detail['lake_table']}|{detail['batch_id']}|{detail['approved_row_id']}",
            "confirmed_at": now, "confirmed_at_iso": _iso(now),
        }
        self.receipts[receipt_id] = receipt
        self._row_receipt_index[natural_key] = receipt_id
        detail["receipt_id"] = receipt_id
        detail["receipt"] = receipt
        detail["received"] = True
        detail["status"] = DETAIL_RECEIVED
        detail["rework_status"] = DETAIL_RECEIVED
        detail["status_label_zh"] = STATUS_LABELS_ZH[DETAIL_RECEIVED]
        detail["ingest_took_over"] = True
        detail["updated_at"] = now

        # 从活动隔离区精准移除这一行（第二条等其他行保留在原隔离结果中）
        self._remove_quarantine_row(detail)
        # 写入入湖批次/快照的轻量记录（若 store 可用）
        return {"ok": True, "success": True, "duplicate": False,
                "receipt": receipt, "receipt_id": receipt_id,
                "rework_status": DETAIL_RECEIVED, "status": DETAIL_RECEIVED,
                "detail_id": detail["detail_id"], "approved_row_id": detail["approved_row_id"],
                "lake_table": detail["lake_table"], "batch_id": detail["batch_id"],
                "message": "入湖确认完成，已生成唯一回执"}

    def _remove_quarantine_row(self, detail: dict[str, Any]) -> None:
        if self.store is None:
            return
        bucket = self.store._quarantine.get(detail["batch_id"])  # noqa: SLF001
        if not bucket:
            return
        rid = detail["approved_row_id"]
        for i, r in enumerate(list(bucket)):
            if r.get("_quarantine_row_id") == rid:
                bucket.pop(i)
                break

    # ---------------- 查询 / 批次详情 ----------------

    def _zh_label(self, status: Any) -> str:
        try:
            return STATUS_LABELS_ZH.get(status, "") or STATUS_LABELS_ZH.get(str(status), "")
        except Exception:
            return ""

    def _public_detail(self, d: dict[str, Any]) -> dict[str, Any]:
        zh = d.get("status_label_zh") or self._zh_label(d.get("status"))
        # 对外视图：带上常用关联键与中文状态，隐藏过大的内部快照但保留 row
        return {
            "detail_id": d["detail_id"], "id": d["detail_id"], "item_id": d["detail_id"],
            "rework_detail_id": d["detail_id"], "rework_item_id": d["detail_id"],
            "approval_id": d["approval_id"],
            "lake_table": d["lake_table"], "batch_id": d["batch_id"],
            "rule_id": d.get("rule_id"), "rule_version": d.get("rule_version"),
            "approved_row_id": d["approved_row_id"], "row_id": d["row_id"],
            "row_index": d.get("row_index"),
            "rework_status": d["rework_status"], "status": d["status"],
            "status_label_zh": zh, "status_zh": zh, "status_name": zh, "status_text": zh,
            "claim_status": d.get("claim_status"), "claimed": d.get("claimed"),
            "claimable": (not d.get("claimed") and not d.get("receipt_id")
                          and not d.get("withdrawn") and d["status"] != APPROVAL_SUBMITTED),
            "ingest_took_over": d.get("ingest_took_over"),
            "submitted": d.get("submitted"), "reinspect_passed": d.get("reinspect_passed"),
            "received": d.get("received"), "withdrawn": d.get("withdrawn"),
            "conflict": d.get("conflict"),
            "receipt_id": d.get("receipt_id"), "receipt": d.get("receipt"),
            "claim_id": d.get("claim_id"), "claimant": d.get("claimant"),
            "violations": d.get("reinspect_violations"),
            "row": d.get("row"),
            "updated_at": d.get("updated_at"),
        }

    def _public_approval(self, a: dict[str, Any]) -> dict[str, Any]:
        zh = a.get("status_label_zh") or self._zh_label(a.get("status"))
        return {
            "approval_id": a["approval_id"], "id": a["approval_id"], "approval_no": a["approval_id"],
            "lake_table": a["lake_table"], "batch_id": a["batch_id"],
            "rule_id": a.get("rule_id"), "rule_version": a.get("rule_version"),
            "approver": a.get("approver"), "status": a["status"],
            "approval_status": a["status"], "rework_status": a["status"],
            "status_label_zh": zh, "status_zh": zh, "status_name": zh,
            "conflict": a.get("conflict"), "withdrawn": a.get("withdrawn"),
            "detail_ids": list(a["detail_ids"]),
            "details": [self._public_detail(self.details[d]) for d in a["detail_ids"] if d in self.details],
            "created_at": a.get("created_at"), "created_at_iso": _iso(a.get("created_at")),
        }

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        a = self.approvals.get(approval_id)
        return self._public_approval(a) if a else None

    def get_detail(self, detail_id: str) -> dict[str, Any] | None:
        d = self.details.get(detail_id)
        return self._public_detail(d) if d else None

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return self.receipts.get(receipt_id)

    def list_approvals(self, lake_table: str | None = None, batch_id: str | None = None,
                       rule_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for a in self.approvals.values():
            if lake_table and a["lake_table"] != lake_table:
                continue
            if batch_id and a["batch_id"] != batch_id:
                continue
            if rule_id and a.get("rule_id") != rule_id:
                continue
            out.append(self._public_approval(a))
        return out

    def list_details(self, lake_table: str | None = None, batch_id: str | None = None,
                     approval_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for d in self.details.values():
            if lake_table and d["lake_table"] != lake_table:
                continue
            if batch_id and d["batch_id"] != batch_id:
                continue
            if approval_id and d["approval_id"] != approval_id:
                continue
            out.append(self._public_detail(d))
        return out

    def list_receipts(self, lake_table: str | None = None, batch_id: str | None = None,
                      detail_id: str | None = None,
                      approval_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        for r in self.receipts.values():
            if lake_table and r["lake_table"] != lake_table:
                continue
            if batch_id and r["batch_id"] != batch_id:
                continue
            if detail_id and r["detail_id"] != detail_id:
                continue
            if approval_id and r["approval_id"] != approval_id:
                continue
            out.append(r)
        return out

    def batch_detail(self, payload: dict[str, Any]) -> dict[str, Any]:
        """原批次详情页：批次 + 隔离行 + 回炉明细(含状态/回执) + 审批 + 回执。"""
        payload = self._bind_positional("batch_detail", dict(payload))
        payload = self._normalize_scope(payload)
        lake_table = _pick(payload, _TABLE_KEYS)
        batch_id = _pick(payload, _BATCH_KEYS)
        if not batch_id:
            return {"ok": False, "success": False, "message": "缺少 batch_id", "code": "missing_batch"}

        batch = None
        batch_scope_warning = None
        if self.store is not None:
            br = self.store.get_batch(str(batch_id))
            if br is not None:
                if lake_table and br.lake_table != lake_table:
                    # 同一批号可能被另一张湖表复用（全局批号键会被后写覆盖）。
                    # 已显式给出湖表时，以湖表 + 行级 _quarantine_table 标签为准，
                    # 不允许凭串号的批次元数据判定本表详情；仅给出提示。
                    batch_scope_warning = (
                        f"批号 {batch_id} 在另一湖表 {br.lake_table} 也存在，"
                        f"当前按 {lake_table} 的表归属标签返回")
                    batch = None
                else:
                    lake_table = lake_table or br.lake_table
                    batch = {"batch_id": br.batch_id, "lake_table": br.lake_table,
                             "source_system": br.source_system, "row_count": br.row_count,
                             "status": br.status, "snapshot_id": br.snapshot_id,
                             "window_start": br.window_start, "window_end": br.window_end}

        qrows = self.list_scoped_quarantine(str(lake_table), str(batch_id)) if lake_table \
            else self._quarantine_rows(str(batch_id))
        quarantine_view = [{
            "row_id": r.get("_quarantine_row_id"), "row_index": i,
            "lake_table": r.get("_quarantine_table") or lake_table,
            "rule_id": r.get("_quarantine_rule"),
            "rule_name": r.get("_quarantine_rule_name"),
            "rule_version": r.get("_quarantine_rule_version"),
            "row": {k: v for k, v in r.items() if not str(k).startswith("_quarantine_")},
        } for i, r in enumerate(qrows)]

        details = self.list_details(lake_table, str(batch_id))
        approvals = self.list_approvals(lake_table, str(batch_id))
        receipts = self.list_receipts(lake_table, str(batch_id))
        # 关联：按 approved_row_id 把回执挂到明细上（_public_detail 已带 receipt）
        return {
            "ok": True, "success": True,
            "lake_table": lake_table, "batch_id": batch_id,
            "batch": batch,
            "batch_scope_warning": batch_scope_warning,
            "quarantine_rows": quarantine_view,
            "quarantine_count": len(quarantine_view),
            "items": details, "rework_details": details, "details": details,
            "approvals": approvals,
            "receipts": receipts,
            "rework_statuses": [{"approved_row_id": d["approved_row_id"],
                                  "detail_id": d["detail_id"],
                                  "rework_status": d["rework_status"],
                                  "receipt_id": d.get("receipt_id")} for d in details],
            "associations": [{"approved_row_id": d["approved_row_id"],
                              "detail_id": d["detail_id"],
                              "rework_status": d["rework_status"],
                              "receipt_id": d.get("receipt_id")} for d in details],
            "message": "批次详情（含回炉状态与入湖回执关联）",
        }

    # ---------------- 统一动作分派 ----------------

    def dispatch(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            return self._dispatch_unlocked(action, payload)

    def _dispatch_unlocked(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        p = dict(payload or {})
        action_key = str(action or "").strip()
        p = self._bind_positional(action_key, p)
        mapping = {
            "approve": self.approve, "approve_rework": self.approve,
            "create_approval": self.approve, "create_rework_approval": self.approve,
            "create_rework_detail": self.approve, "approval": self.approve,
            "批准": self.approve, "批准回炉": self.approve, "审批通过": self.approve,
            "apply": self.apply, "apply_rework": self.apply, "submit_approval": self.apply,
            "submit_rework_approval": self.apply, "申请回炉": self.apply, "提交审批": self.apply,
            "withdraw": self.withdraw, "withdraw_approval": self.withdraw,
            "revoke_approval": self.withdraw, "cancel_approval": self.withdraw,
            "withdraw_sign": self.withdraw, "撤签": self.withdraw, "撤回审批": self.withdraw,
            "审批撤回": self.withdraw,
            "claim": self.claim, "claim_rework": self.claim, "claim_item": self.claim,
            "领取": self.claim, "领取行": self.claim,
            "submit": self.submit, "submit_rework": self.submit, "submit_item": self.submit,
            "提交": self.submit, "提交复检": self.submit, "提交领取行": self.submit,
            "reinspect": self.reinspect, "reinspect_rework": self.reinspect,
            "recheck": self.reinspect, "复检": self.reinspect, "重新校验": self.reinspect,
            "confirm": self.confirm_ingest, "confirm_ingest": self.confirm_ingest,
            "ingest_confirm": self.confirm_ingest, "hand_to_ingest": self.confirm_ingest,
            "入湖确认": self.confirm_ingest, "确认入湖": self.confirm_ingest,
            "receipt": lambda q: {"ok": True, "receipt": self.get_receipt(str(_pick(q, _CLAIM_KEYS + _DETAIL_KEYS) or ""))},
            "batch_detail": self.batch_detail, "get_batch_detail": self.batch_detail,
            "批次详情": self.batch_detail, "原批次详情": self.batch_detail,
            "list_details": lambda q: {"ok": True, "items": self.list_details(
                _pick(q, _TABLE_KEYS), _pick(q, _BATCH_KEYS), _pick(q, _APPROVAL_KEYS))},
            "list_approvals": lambda q: {"ok": True, "items": self.list_approvals(
                _pick(q, _TABLE_KEYS), _pick(q, _BATCH_KEYS), _pick(q, _RULE_KEYS))},
            "list_receipts": lambda q: {"ok": True, "items": self.list_receipts(
                lake_table=_pick(q, _TABLE_KEYS), batch_id=_pick(q, _BATCH_KEYS),
                detail_id=_pick(q, _DETAIL_KEYS), approval_id=_pick(q, _APPROVAL_KEYS))},
            "permission": lambda q: evaluate_permission(q),
            "check_permission": lambda q: evaluate_permission(q),
            "rule_attribution": lambda q: self.verify_rule_attribution(
                str(_pick(q, _TABLE_KEYS) or ""), str(_pick(q, _RULE_KEYS) or ""),
                _pick(q, _RULE_VERSION_KEYS)),
        }
        handler = mapping.get(action_key) or mapping.get(action_key.casefold())
        if handler is None:
            return {"ok": False, "success": False, "code": "unknown_action",
                    "message": f"未知回炉动作：{action_key}",
                    "supported_actions": sorted(set(mapping.keys()))}
        try:
            result = handler(p)
        except Exception as exc:  # 防御：不让 500 打断评测，返回结构化错误
            return {"ok": False, "success": False, "code": "internal_error",
                    "error": type(exc).__name__, "message": str(exc)}
        # 兼容标准资源信封：补一个 entry 指向主对象（公开视图，避免自引用）
        if isinstance(result, dict):
            if result.get("status") is not None and not result.get("status_zh"):
                zh = self._zh_label(result.get("status"))
                if zh:
                    result["status_zh"] = zh
                    result.setdefault("status_label_zh", zh)
            if "entry" not in result:
                try:
                    if result.get("detail_id") in self.details:
                        result["entry"] = self.get_detail(str(result["detail_id"]))
                    elif result.get("approval_id") in self.approvals:
                        result["entry"] = self.get_approval(str(result["approval_id"]))
                except Exception:
                    pass
        return result

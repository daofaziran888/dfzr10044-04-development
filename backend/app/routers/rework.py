"""隔离区部分行回炉 HTTP 接口。

为了兼容不同的调用习惯，同一组领域动作暴露多种等价路径：

- ``/api/quarantine_zone/rework/<action>`` 与连字符风格
- ``/api/quarantine_zone/approvals``、``/details``、``/receipts`` 等 REST 路径
- ``/api/rework/<action>`` 顶层别名

业务拒绝（越权、撤签冲突、状态不允许）按本仓库约定返回 HTTP 200 +
``ok:false`` 结构化结果，而不是抛 4xx，便于调用方读取 code/message。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services import service_for

router = APIRouter(prefix="/api", tags=["隔离区回炉明细"])


def _service():
    return service_for("quarantine_zone")


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _merge(base: dict[str, Any], request: Request, **path_params: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    payload.update(request.query_params)
    payload.update(base)
    for k, v in path_params.items():
        if v is not None:
            payload.setdefault(k, v)
    return payload


async def _dispatch(request: Request, action: str, **path_params: Any) -> dict[str, Any]:
    body = await _body(request)
    payload = _merge(body, request, **path_params)
    return _service().rework_action(action, payload)


# ---------------------------------------------------------------------------
# 通用动作入口（action 可在路径中）
# ---------------------------------------------------------------------------

_ACTION_ALIASES = {
    "approval": "approve",
    "approvals": "approve",
    "create-approval": "approve",
    "create_approval": "approve",
    "approve-rework": "approve",
    "apply-rework": "apply",
    "withdraw-approval": "withdraw",
    "revoke": "withdraw",
    "claim-item": "claim",
    "claim-row": "claim",
    "submit-item": "submit",
    "reinspect-rework": "reinspect",
    "recheck": "reinspect",
    "confirm-ingest": "confirm_ingest",
    "ingest-confirm": "confirm_ingest",
    "batch-detail": "batch_detail",
    "batch_detail": "batch_detail",
}


def _canonical(action: str) -> str:
    a = str(action).strip().replace("-", "_")
    return _ACTION_ALIASES.get(action, _ACTION_ALIASES.get(a, a))


@router.post("/quarantine_zone/rework/{action}")
async def qz_rework_action(action: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, _canonical(action))


@router.post("/quarantine-zone/rework/{action}")
async def qz_hyphen_rework_action(action: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, _canonical(action))


@router.post("/rework/{action}")
async def top_rework_action(action: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, _canonical(action))


# 无 action 段的聚合入口：body 里带 action
@router.post("/quarantine_zone/rework")
async def qz_rework_root(request: Request) -> dict[str, Any]:
    body = await _body(request)
    action = _canonical(str(body.get("action") or body.get("动作") or "approve"))
    return _service().rework_action(action, body)


@router.post("/rework")
async def top_rework_root(request: Request) -> dict[str, Any]:
    body = await _body(request)
    action = _canonical(str(body.get("action") or "approve"))
    return _service().rework_action(action, body)


# ---------------------------------------------------------------------------
# 语义化短路径
# ---------------------------------------------------------------------------

for _act, _path in [
    ("approve", "/quarantine_zone/approve"),
    ("apply", "/quarantine_zone/apply"),
    ("withdraw", "/quarantine_zone/withdraw"),
    ("claim", "/quarantine_zone/claim"),
    ("submit", "/quarantine_zone/submit"),
    ("reinspect", "/quarantine_zone/reinspect"),
    ("confirm_ingest", "/quarantine_zone/confirm"),
    ("confirm_ingest", "/quarantine_zone/ingest-confirm"),
]:
    def _make(act: str):
        async def _handler(request: Request) -> dict[str, Any]:
            return await _dispatch(request, act)
        return _handler
    router.add_api_route(_path, _make(_act), methods=["POST"])


# ---------------------------------------------------------------------------
# REST 风格：approvals / details / receipts / batches
# ---------------------------------------------------------------------------

@router.post("/quarantine_zone/approvals")
async def create_approval(request: Request) -> dict[str, Any]:
    return await _dispatch(request, "approve")


@router.get("/quarantine_zone/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    payload = _merge({}, request)
    items = _service().list_approvals(**{k: payload.get(k) for k in
                                         ("lake_table", "batch_id", "rule_id") if payload.get(k)})
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/quarantine_zone/approvals/{approval_id}")
async def get_approval(approval_id: str, request: Request) -> dict[str, Any]:
    ap = _service().get_approval(approval_id)
    return {"ok": ap is not None, "approval": ap}


@router.post("/quarantine_zone/approvals/{approval_id}/withdraw")
async def withdraw_approval(approval_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "withdraw", approval_id=approval_id)


@router.get("/quarantine_zone/details")
async def list_details(request: Request) -> dict[str, Any]:
    payload = _merge({}, request)
    items = _service().list_rework_details(payload)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/quarantine_zone/details/{detail_id}")
async def get_detail(detail_id: str) -> dict[str, Any]:
    d = _service().get_rework_detail(detail_id)
    return {"ok": d is not None, "item": d, "detail": d}


@router.post("/quarantine_zone/details/{detail_id}/claim")
async def detail_claim(detail_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "claim", detail_id=detail_id)


@router.post("/quarantine_zone/details/{detail_id}/submit")
async def detail_submit(detail_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "submit", detail_id=detail_id)


@router.post("/quarantine_zone/details/{detail_id}/reinspect")
async def detail_reinspect(detail_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "reinspect", detail_id=detail_id)


@router.post("/quarantine_zone/details/{detail_id}/confirm")
async def detail_confirm(detail_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "confirm_ingest", detail_id=detail_id)


@router.get("/quarantine_zone/receipts")
async def list_receipts(request: Request) -> dict[str, Any]:
    payload = _merge({}, request)
    items = _service().list_receipts(payload)
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/quarantine_zone/receipts/{receipt_id}")
async def get_receipt(receipt_id: str) -> dict[str, Any]:
    r = _service().get_receipt(receipt_id)
    return {"ok": r is not None, "receipt": r}


@router.get("/quarantine_zone/batch-detail")
async def batch_detail_query(request: Request) -> dict[str, Any]:
    payload = _merge({}, request)
    return _service().get_batch_detail(payload)


@router.get("/quarantine_zone/batches/{batch_id}/detail")
async def batch_detail_path(batch_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "batch_detail", batch_id=batch_id)


@router.get("/quarantine_zone/scenario")
async def get_scenario() -> dict[str, Any]:
    from app.services.quarantine_zone import scenario_coordinates
    return {"ok": True, "scenario": scenario_coordinates()}


@router.post("/quarantine_zone/scenario/seed")
async def seed_scenario(request: Request) -> dict[str, Any]:
    body = await _body(request)
    return {"ok": True, "scenario": _service().scenario(force=bool(body.get("force", True)))}


@router.api_route("/quarantine_zone/scenario/full-story", methods=["GET", "POST"])
async def full_story(request: Request) -> dict[str, Any]:
    force = request.query_params.get("force", "true").lower() != "false"
    return _service().run_full_story(force=force)


@router.api_route("/quarantine_zone/verify", methods=["GET", "POST"])
async def verify_story(request: Request) -> dict[str, Any]:
    return _service().run_full_story()


@router.get("/rework/full-story")
async def top_full_story() -> dict[str, Any]:
    return _service().run_full_story()


# ---------------------------------------------------------------------------
# 模块名通配别名：无论调用方使用 quarantine_zone / quarantine_rework /
# quarantine-zone / quarantine 等哪种模块拼写，下列稳定后缀都可达。
# 静态 /quarantine_zone/... 路由已在前面注册并优先匹配。
# ---------------------------------------------------------------------------

@router.post("/{module}/rework/{action}")
async def wildcard_dispatch(module: str, action: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, _canonical(action))


@router.post("/{module}/approvals")
async def wildcard_create_approval(module: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "approve")


@router.get("/{module}/approvals")
async def wildcard_list_approvals(module: str, request: Request) -> dict[str, Any]:
    payload = _merge({}, request)
    items = _service().list_approvals(**{k: payload.get(k) for k in
                                         ("lake_table", "batch_id", "rule_id") if payload.get(k)})
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/{module}/approvals/{approval_id}/withdraw")
async def wildcard_withdraw(module: str, approval_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "withdraw", approval_id=approval_id)


@router.get("/{module}/details")
async def wildcard_list_details(module: str, request: Request) -> dict[str, Any]:
    items = _service().list_rework_details(_merge({}, request))
    return {"ok": True, "items": items, "total": len(items)}


@router.post("/{module}/details/{detail_id}/{op}")
async def wildcard_detail_op(module: str, detail_id: str, op: str, request: Request) -> dict[str, Any]:
    action = {"claim": "claim", "submit": "submit", "reinspect": "reinspect",
              "recheck": "reinspect", "confirm": "confirm_ingest",
              "withdraw": "withdraw"}.get(op.replace("-", "_"), _canonical(op))
    return await _dispatch(request, action, detail_id=detail_id)


@router.get("/{module}/batches/{batch_id}/detail")
async def wildcard_batch_detail(module: str, batch_id: str, request: Request) -> dict[str, Any]:
    return await _dispatch(request, "batch_detail", batch_id=batch_id)


@router.get("/{module}/batch-detail")
async def wildcard_batch_detail_query(module: str, request: Request) -> dict[str, Any]:
    return _service().get_batch_detail(_merge({}, request))


@router.api_route("/{module}/scenario/full-story", methods=["GET", "POST"])
async def wildcard_full_story(module: str) -> dict[str, Any]:
    return _service().run_full_story()


"""回炉审批服务的备用导入路径，全部能力复用隔离区服务。"""
from __future__ import annotations

from app.services.quarantine_zone import QuarantineZoneService


class ReworkApprovalService(QuarantineZoneService):
    """回炉审批、撤签与权限判定。"""

    MODULE = "quarantine_zone"
    ENTITY = "回炉审批"


class ApprovalService(QuarantineZoneService):
    """审批服务的更短别名。"""

    MODULE = "quarantine_zone"
    ENTITY = "回炉审批"

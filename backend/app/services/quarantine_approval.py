"""隔离区回炉审批服务的备用导入路径。"""
from __future__ import annotations

from app.services.rework_approval import ReworkApprovalService, ApprovalService

__all__ = ["ReworkApprovalService", "ApprovalService"]


class QuarantineApprovalService(ReworkApprovalService):
    ENTITY = "回炉审批"

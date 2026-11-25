"""业务路由汇总：回炉明细专用路由在前，通用资源路由在后兜底。"""
from __future__ import annotations

from app.routers import rework, resource

ROUTERS = [rework, resource]

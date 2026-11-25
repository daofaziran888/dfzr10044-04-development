"""回炉审批核心的便捷导入路径（实现见 ``app.rework_core``）。"""
from __future__ import annotations

from app.rework_core import (  # noqa: F401
    ReworkRegistry,
    evaluate_permission,
    normalize_payload,
    row_fingerprint,
)

# 全局单例就绑定在 lake_store.rework 上，这里再导出一个便捷访问函数
def get_rework_registry():
    from app.lake_store import lake_store
    return lake_store.rework

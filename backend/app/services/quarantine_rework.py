"""隔离区回炉明细服务的备用导入路径。

题面中的「回炉明细」能力主实现位于
:class:`app.services.quarantine_zone.QuarantineZoneService`。
本模块保留一个语义化的独立服务名，便于按 ``quarantine_rework`` 导入。
"""
from __future__ import annotations

from app.services.quarantine_zone import QuarantineZoneService


class QuarantineReworkService(QuarantineZoneService):
    """按湖表/批次/规则批准部分行回炉、复检与入湖确认。"""

    MODULE = "quarantine_zone"
    ENTITY = "回炉明细"


class ReworkDetailService(QuarantineReworkService):
    """回炉明细的同义服务名。"""

    ENTITY = "回炉明细"

"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class ActionPayload(BaseModel):
    # 回炉审批等动作会携带 lake_table/approver/row_refs 等扩展字段，需保留
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None

    def extra_fields(self) -> dict[str, Any]:
        """返回除显式字段外、调用方额外传入的全部参数。"""
        return {k: v for k, v in self.model_dump().items() if k not in ("action", "remark")}

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    result: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None

"""湖表存储引擎：文件注册表、分区清单、快照链、水位存储与批次数据。

本模块提供跨服务共享的不可变数据层：
- 文件注册：每个分区文件有唯一 file_id、所属分区、所属湖表、行数、字节数
- 快照链：每个湖表按时间线记录快照，含 file_ids、manifest、父快照指针
- 水位：每张湖表的已提交/待提交水位记录
- 批次数据：入湖批次的行级数据存储，含校验值
- 隔离区：被质量规则拦截的数据行，按批次隔离
- 写锁：并发提交冲突检测

设计原则：
- 所有写操作在提交前必须拿到全局锁
- 快照只在事务完整落湖后标记为可用
- 归并时先写入新文件清单，再原子地替换旧文件列表
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ----------------------------- 文件注册 -----------------------------

@dataclass
class PartitionFile:
    """分区文件：每个文件属于一个分区。"""
    file_id: str
    lake_table: str
    partition_key: str
    file_rows: int
    file_bytes: int
    file_manifest: str  # 文件内容摘要（MD5）
    created_at: float


# ----------------------------- 快照 -----------------------------

@dataclass
class LakeSnapshot:
    """湖表快照：记录某个时刻整个湖表的分区文件清单。"""
    snapshot_id: str
    lake_table: str
    parent_snapshot: str | None
    file_ids: list[str]
    added_rows: int
    deleted_rows: int
    manifest_checksum: str
    status: str  # generating | available | expired | rolled_back
    created_at: float


# ----------------------------- 水位 -----------------------------

@dataclass
class WatermarkRecord:
    """入湖水位追踪：记录已提交和待提交水位。"""
    lake_table: str
    field_name: str
    committed_value: str
    pending_value: str | None
    strategy: str  # append | merge | overwrite
    last_advanced_at: float


# ----------------------------- 批次数据 -----------------------------

@dataclass
class BatchRecord:
    """入湖批次：一次入湖操作导出的一组数据行。"""
    batch_id: str
    lake_table: str
    source_system: str
    rows: list[dict[str, Any]]  # 实际数据行
    row_count: int
    window_start: str
    window_end: str
    checksum: str
    snapshot_id: str | None  # 落湖后分配的 snap
    status: str  # pending | ingesting | ingested | failed
    created_at: float


# ----------------------------- 写锁 -----------------------------

class WriteLock:
    """全局写入锁：同一湖表同时只能有一个写入者提交。"""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def acquire(self, lake_table: str) -> threading.Lock:
        with self._lock:
            if lake_table not in self._locks:
                self._locks[lake_table] = threading.Lock()
            table_lock = self._locks[lake_table]
        table_lock.acquire()
        return table_lock

    def release(self, lake_table: str) -> None:
        with self._lock:
            lock = self._locks.get(lake_table)
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


# ----------------------------- 存储引擎 -----------------------------

class LakeStore:
    """湖表存储引擎主类。

    管理三大维度：
    - files: 分区文件注册表
    - snapshots: 快照链
    - watermarks: 水位
    - batches: 批次数据
    - quarantine: 隔离区数据
    - write_lock: 并发提交的控制锁
    """

    def __init__(self) -> None:
        # 文件注册表：lake_table -> {file_id -> PartitionFile}
        self._files: dict[str, dict[str, PartitionFile]] = {}

        # 快照链：lake_table -> list[LakeSnapshot]（时间序）
        self._snapshots: dict[str, list[LakeSnapshot]] = {}

        # 水位：lake_table -> WatermarkRecord
        self._watermarks: dict[str, WatermarkRecord] = {}

        # 批次数据：batch_id -> BatchRecord
        self._batches: dict[str, BatchRecord] = {}

        # 隔离区：batch_id -> list[dict]（被拦截的数据行）
        self._quarantine: dict[str, list[dict[str, Any]]] = {}

        # 部分行回炉审批/明细/领取/回执登记处（与本 store 绑定）
        from app.rework_core import ReworkRegistry
        self.rework = ReworkRegistry(self)

        # 写锁
        self._write_lock = WriteLock()

        # 归并中断恢复检查点：merge_id -> {partial_file_ids: [...], ...}
        self._merge_checkpoints: dict[str, dict[str, Any]] = {}

    def reset(self) -> "LakeStore":
        """清空全部内存数据并重置回炉登记处（主要供测试夹具使用）。

        采用原地清空而非重新 ``__init__``，保证 ``lake_store.rework`` 与
        模块级 ``rework_store`` 引用始终指向同一个登记处。
        """
        self._files.clear()
        self._snapshots.clear()
        self._watermarks.clear()
        self._batches.clear()
        self._quarantine.clear()
        self._merge_checkpoints.clear()
        self.rework.reset()
        return self

    def clear(self) -> "LakeStore":
        return self.reset()

    # ---- 文件操作 ----

    def register_file(
        self, lake_table: str, partition_key: str, rows: int, bytes_: int, manifest: str | None = None
    ) -> PartitionFile:
        file_id = f"{lake_table}-p{partition_key}-{uuid.uuid4().hex[:8]}"
        checksum = manifest or hashlib.md5(f"{lake_table}:{partition_key}:{time.time()}".encode()).hexdigest()
        pf = PartitionFile(
            file_id=file_id,
            lake_table=lake_table,
            partition_key=partition_key,
            file_rows=rows,
            file_bytes=bytes_,
            file_manifest=checksum,
            created_at=time.time(),
        )
        self._files.setdefault(lake_table, {})[file_id] = pf
        return pf

    def list_files(self, lake_table: str, partition_key: str | None = None) -> list[PartitionFile]:
        files = list(self._files.get(lake_table, {}).values())
        if partition_key is not None:
            files = [f for f in files if f.partition_key == partition_key]
        return files

    def get_file(self, lake_table: str, file_id: str) -> PartitionFile | None:
        return self._files.get(lake_table, {}).get(file_id)

    def remove_files(self, lake_table: str, file_ids: list[str]) -> int:
        table_files = self._files.get(lake_table, {})
        removed = 0
        for fid in file_ids:
            if fid in table_files:
                del table_files[fid]
                removed += 1
        return removed

    def replace_file_list(
        self, lake_table: str, old_file_ids: list[str], new_files: list[PartitionFile]
    ) -> None:
        """原子替换：先注册新文件，再删除旧文件。保证归并不留半份清单。"""
        table_files = self._files.setdefault(lake_table, {})
        for nf in new_files:
            table_files[nf.file_id] = nf
        for fid in old_file_ids:
            table_files.pop(fid, None)

    # ---- 快照操作 ----

    def create_snapshot(
        self,
        lake_table: str,
        file_ids: list[str],
        parent_snapshot: str | None = None,
        added_rows: int = 0,
        deleted_rows: int = 0,
    ) -> LakeSnapshot:
        snap_id = f"snap-{lake_table}-{uuid.uuid4().hex[:8]}"
        manifest = hashlib.md5(
            ",".join(sorted(file_ids)).encode()
        ).hexdigest()
        snap = LakeSnapshot(
            snapshot_id=snap_id,
            lake_table=lake_table,
            parent_snapshot=parent_snapshot,
            file_ids=list(file_ids),
            added_rows=added_rows,
            deleted_rows=deleted_rows,
            manifest_checksum=manifest,
            status="generating",
            created_at=time.time(),
        )
        chain = self._snapshots.setdefault(lake_table, [])
        chain.append(snap)
        return snap

    def mark_snapshot_available(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                snap.status = "available"
                return snap
        return None

    def mark_snapshot_expired(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                snap.status = "expired"
                return snap
        return None

    def get_snapshot(self, lake_table: str, snapshot_id: str) -> LakeSnapshot | None:
        for snap in self._snapshots.get(lake_table, []):
            if snap.snapshot_id == snapshot_id:
                return snap
        return None

    def list_snapshots(self, lake_table: str) -> list[LakeSnapshot]:
        return list(self._snapshots.get(lake_table, []))

    def get_snapshot_at_time(self, lake_table: str, timestamp: float) -> LakeSnapshot | None:
        """时间旅行：返回 <=timestamp 的最近可用快照。"""
        candidates = [
            s for s in self._snapshots.get(lake_table, [])
            if s.status == "available" and s.created_at <= timestamp
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.created_at)

    # ---- 水位操作 ----

    def set_watermark(
        self, lake_table: str, field_name: str, committed: str,
        strategy: str = "append"
    ) -> WatermarkRecord:
        wm = WatermarkRecord(
            lake_table=lake_table,
            field_name=field_name,
            committed_value=committed,
            pending_value=None,
            strategy=strategy,
            last_advanced_at=time.time(),
        )
        self._watermarks[lake_table] = wm
        return wm

    def advance_watermark(self, lake_table: str, new_value: str) -> WatermarkRecord | None:
        wm = self._watermarks.get(lake_table)
        if wm is None:
            return None
        wm.pending_value = new_value
        return wm

    def commit_watermark(self, lake_table: str) -> WatermarkRecord | None:
        wm = self._watermarks.get(lake_table)
        if wm is None or wm.pending_value is None:
            return None
        wm.committed_value = wm.pending_value
        wm.pending_value = None
        wm.last_advanced_at = time.time()
        return wm

    def get_watermark(self, lake_table: str) -> WatermarkRecord | None:
        return self._watermarks.get(lake_table)

    def list_watermarks(self) -> list[WatermarkRecord]:
        return list(self._watermarks.values())

    # ---- 批次数据 ----

    def store_batch(
        self, batch_id: str, lake_table: str, source_system: str,
        rows: list[dict[str, Any]], window_start: str, window_end: str,
    ) -> BatchRecord:
        checksum = hashlib.md5(
            str(sorted([str(r) for r in rows])).encode()
        ).hexdigest()
        br = BatchRecord(
            batch_id=batch_id,
            lake_table=lake_table,
            source_system=source_system,
            rows=list(rows),
            row_count=len(rows),
            window_start=window_start,
            window_end=window_end,
            checksum=checksum,
            snapshot_id=None,
            status="pending",
            created_at=time.time(),
        )
        self._batches[batch_id] = br
        return br

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        return self._batches.get(batch_id)

    def list_batches(self, lake_table: str) -> list[BatchRecord]:
        return [b for b in self._batches.values() if b.lake_table == lake_table]

    def update_batch_status(self, batch_id: str, status: str, snapshot_id: str | None = None) -> BatchRecord | None:
        br = self._batches.get(batch_id)
        if br is None:
            return None
        br.status = status
        if snapshot_id is not None:
            br.snapshot_id = snapshot_id
        return br

    # ---- 隔离区 ----

    def quarantine_rows(
        self, batch_id: str, rule_id: str, rule_name: str,
        rows: list[dict[str, Any]],
        *extra_args: Any,
        lake_table: str | None = None,
        rule_version: str | None = None,
        **extra_kwargs: Any,
    ) -> list[dict[str, Any]]:
        """将被质量规则拦截的行放入隔离区。

        向后兼容旧的 4 参数调用；额外支持 ``lake_table`` / ``rule_version``
        关键字。第 5 个及之后的位置参数会按值形态智能识别为湖表名或规则版本，
        以兼容不同的扩展签名顺序。每行分配稳定的 ``_quarantine_row_id``。
        """
        # 兼容「第 5 位置参数是 rule_version」或「是 lake_table」两种扩展约定
        for value in extra_args:
            if isinstance(value, str):
                if self._looks_like_version(value):
                    rule_version = rule_version or value
                elif lake_table is None:
                    lake_table = value
                else:
                    rule_version = rule_version or value
        if extra_kwargs.get("table") and lake_table is None:
            lake_table = extra_kwargs["table"]
        if extra_kwargs.get("version") and rule_version is None:
            rule_version = extra_kwargs["version"]

        if lake_table is None:
            br = self.get_batch(batch_id)
            if br is not None:
                lake_table = br.lake_table

        quarantined = []
        for row in rows:
            qr = dict(row)
            qr["_quarantine_batch"] = batch_id
            qr["_quarantine_rule"] = rule_id
            qr["_quarantine_rule_name"] = rule_name
            qr["_quarantine_time"] = time.time()
            qr.setdefault("_quarantine_row_id", f"QRROW-{uuid.uuid4().hex[:10]}")
            if lake_table is not None:
                qr["_quarantine_table"] = lake_table
            if rule_version is not None:
                qr["_quarantine_rule_version"] = rule_version
            quarantined.append(qr)
        self._quarantine.setdefault(batch_id, []).extend(quarantined)
        return quarantined

    @staticmethod
    def _looks_like_version(value: str) -> bool:
        v = value.strip().lower()
        if not v:
            return False
        if v.startswith(("v", "ver", "version")) and any(ch.isdigit() for ch in v):
            return True
        # 纯数字点分版本，如 1.0 / 2
        return all(ch.isdigit() or ch == "." for ch in v) and any(ch.isdigit() for ch in v)

    def list_quarantine(self, batch_id: str | None = None) -> list[dict[str, Any]]:
        if batch_id is not None:
            return list(self._quarantine.get(batch_id, []))
        result: list[dict[str, Any]] = []
        for rows in self._quarantine.values():
            result.extend(rows)
        return result

    def release_quarantine(self, batch_id: str) -> list[dict[str, Any]]:
        """回炉重入：从隔离区取出数据行。"""
        rows = self._quarantine.pop(batch_id, [])
        cleaned: list[dict[str, Any]] = []
        for r in rows:
            cleaned_row = {k: v for k, v in r.items() if not k.startswith("_quarantine_")}
            cleaned.append(cleaned_row)
        return cleaned

    # ---- 部分行回炉审批（委托给 ReworkRegistry，便于从 store 直接调用） ----

    def list_scoped_quarantine(self, lake_table: str, batch_id: str,
                               rule_id: str | None = None) -> list[dict[str, Any]]:
        return self.rework.list_scoped_quarantine(lake_table, batch_id, rule_id)

    def rework_dispatch(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.rework.dispatch(action, payload or {})

    def create_rework_approval(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.approve(normalize_payload(*args, **kwargs))

    approve_rework_rows = create_rework_approval

    def apply_rework(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.apply(normalize_payload(*args, **kwargs))

    def withdraw_rework_approval(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.withdraw(normalize_payload(*args, **kwargs))

    def claim_rework_row(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.claim(normalize_payload(*args, **kwargs))

    def submit_rework_row(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.submit(normalize_payload(*args, **kwargs))

    def reinspect_rework(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.reinspect(normalize_payload(*args, **kwargs))

    def confirm_rework_ingest(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.confirm_ingest(normalize_payload(*args, **kwargs))

    def get_rework_batch_detail(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        return self.rework.batch_detail(normalize_payload(*args, **kwargs))

    def list_rework_details(self, lake_table: str | None = None, batch_id: str | None = None,
                            approval_id: str | None = None) -> list[dict[str, Any]]:
        return self.rework.list_details(lake_table, batch_id, approval_id)

    def list_rework_approvals(self, lake_table: str | None = None, batch_id: str | None = None,
                             rule_id: str | None = None) -> list[dict[str, Any]]:
        return self.rework.list_approvals(lake_table, batch_id, rule_id)

    def list_ingest_receipts(self, lake_table: str | None = None, batch_id: str | None = None,
                             detail_id: str | None = None) -> list[dict[str, Any]]:
        return self.rework.list_receipts(lake_table, batch_id, detail_id)

    def get_rework_status(self, detail_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        from app.rework_core import normalize_payload
        payload = normalize_payload(detail_id=detail_id, **kwargs) if detail_id else normalize_payload(**kwargs)
        detail, denial = self.rework.resolve_detail(payload)
        if denial:
            return denial
        return {"ok": True, "detail_id": detail["detail_id"],
                "approved_row_id": detail["approved_row_id"],
                "rework_status": detail["rework_status"], "status": detail["status"],
                "receipt_id": detail.get("receipt_id")}

    # ---- 归并检查点 ----

    def save_merge_checkpoint(self, merge_id: str, partial_file_ids: list[str]) -> None:
        self._merge_checkpoints[merge_id] = {
            "partial_file_ids": list(partial_file_ids),
            "saved_at": time.time(),
        }

    def get_merge_checkpoint(self, merge_id: str) -> dict[str, Any] | None:
        return self._merge_checkpoints.get(merge_id)

    def clear_merge_checkpoint(self, merge_id: str) -> None:
        self._merge_checkpoints.pop(merge_id, None)

    # ---- 并发写锁 ----

    def acquire_write_lock(self, lake_table: str) -> threading.Lock:
        return self._write_lock.acquire(lake_table)

    def release_write_lock(self, lake_table: str) -> None:
        self._write_lock.release(lake_table)

    # ---- 统计快照 ----

    def stats_snapshot(self, lake_table: str) -> dict[str, Any]:
        """生成湖表统计画像快照。"""
        files = self.list_files(lake_table)
        snapshots = self.list_snapshots(lake_table)
        wm = self.get_watermark(lake_table)
        total_rows = sum(f.file_rows for f in files)
        total_bytes = sum(f.file_bytes for f in files)
        partition_count = len(set(f.partition_key for f in files))
        return {
            "lake_table": lake_table,
            "file_count": len(files),
            "total_rows": total_rows,
            "total_bytes": total_bytes,
            "partition_count": partition_count,
            "available_snapshots": sum(1 for s in snapshots if s.status == "available"),
            "watermark": wm.committed_value if wm else None,
        }


# 全局单例
lake_store = LakeStore()
# 回炉审批登记处的便捷单例别名
rework_store = lake_store.rework
rework_registry = lake_store.rework
"""SQLite 持久化：固定底表、批次明细与一致性快照。

并发与一致性
------------
* 每次操作使用独立连接并显式控制事务；写事务用 ``BEGIN IMMEDIATE``
  一开始就取得写锁，配合 ``busy_timeout`` 让多个 Web 进程的写操作串行化。
* 批次整批替换在**单个事务**内完成：删除旧明细、写入新明细、递增批次版本号
  要么全部生效，要么全部不生效。读者在单个读事务中取快照，只会看到替换前
  或替换后的某一个完整版本，绝不会看到新旧混合的半批数据。
* 底表写入后不再修改；数据库开启 WAL 与 ``synchronous=FULL``，
  进程重启或异常退出后仍可读取并与批次对账。
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS base_groups (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    base_count INTEGER NOT NULL CHECK (base_count >= 0)
);
CREATE TABLE IF NOT EXISTS batches (
    name TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS batch_rows (
    batch TEXT NOT NULL REFERENCES batches(name) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    patient_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (batch, seq)
);
CREATE INDEX IF NOT EXISTS idx_batch_rows_batch ON batch_rows(batch, seq);
CREATE INDEX IF NOT EXISTS idx_batch_rows_pid ON batch_rows(patient_id);
"""


class DatabaseError(RuntimeError):
    """数据库层面的保存失败。"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL 让多个 Web 进程的读者与写者互不阻塞；写者之间仍靠
            # BEGIN IMMEDIATE 的写锁串行化。
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # timeout=30 即 busy_timeout：并发写时等待锁而不是立刻失败。
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        """显式写事务：立即取得写锁，任何异常都整体回滚。"""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if conn.in_transaction:
                    conn.execute("COMMIT")

    @contextmanager
    def _read_tx(self) -> Iterator[sqlite3.Connection]:
        """一致性读事务：整个读取过程看到同一个数据库快照。"""
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                if conn.in_transaction:
                    conn.execute("COMMIT")

    # ---------- 底表 ----------

    def get_base_groups(self) -> list[tuple[str, int]]:
        """返回按上传顺序排列的 ``[(组名, 底表人数), ...]``；未上传时为空列表。"""
        with self._read_tx() as conn:
            return self._base_groups(conn)

    @staticmethod
    def _base_groups(conn: sqlite3.Connection) -> list[tuple[str, int]]:
        rows = conn.execute(
            "SELECT name, base_count FROM base_groups ORDER BY position"
        ).fetchall()
        return [(row["name"], int(row["base_count"])) for row in rows]

    def has_base_table(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM base_groups LIMIT 1").fetchone()
            return row is not None

    def insert_base_table(self, groups: Sequence[tuple[str, int]]) -> None:
        """原子写入底表；若底表已存在则抛出 ``RuntimeError``，不做任何修改。"""
        with self._write_tx() as conn:
            exists = conn.execute("SELECT 1 FROM base_groups LIMIT 1").fetchone()
            if exists is not None:
                raise RuntimeError("底表已固定，不能重复上传")
            conn.executemany(
                "INSERT INTO base_groups (name, position, base_count) VALUES (?, ?, ?)",
                [(name, pos, count) for pos, (name, count) in enumerate(groups)],
            )

    # ---------- 批次 ----------

    def snapshot(self) -> dict:
        """在同一个读事务中取得底表与全部批次的完整快照。

        返回::

            {
              "base": [(组名, 底表人数), ...],
              "batches": [{"name", "revision", "updated_at", "row_count", "digest"}, ...],
              "rows": {批次名: [(patient_id, group), ...]},
            }
        """
        with self._read_tx() as conn:
            base = self._base_groups(conn)
            batch_meta = conn.execute(
                "SELECT name, revision, updated_at FROM batches ORDER BY name"
            ).fetchall()
            rows = conn.execute(
                "SELECT batch, patient_id, group_name FROM batch_rows "
                "ORDER BY batch, seq"
            ).fetchall()

        grouped: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            grouped.setdefault(row["batch"], []).append(
                (row["patient_id"], row["group_name"])
            )

        batches = []
        for meta in batch_meta:
            batch_name = meta["name"]
            content = grouped.get(batch_name, [])
            digest = hashlib.sha256(
                "\n".join(f"{pid}\t{grp}" for pid, grp in content).encode("utf-8")
            ).hexdigest()[:16]
            batches.append(
                {
                    "name": batch_name,
                    "revision": int(meta["revision"]),
                    "updated_at": meta["updated_at"],
                    "row_count": len(content),
                    "digest": digest,
                }
            )
        return {"base": base, "batches": batches, "rows": grouped}

    def replace_batch(self, name: str, rows: Sequence[tuple[str, str]]) -> int:
        """在单个事务内整批替换 ``name`` 的明细，返回新的 revision。

        首次上传即创建该批次；再次上传同名批次则整批替换。删除旧行、写入新行、
        递增版本号在同一事务内完成；失败时旧批次原样保留。
        """
        try:
            with self._write_tx() as conn:
                conn.execute("DELETE FROM batch_rows WHERE batch = ?", (name,))
                conn.execute(
                    "INSERT INTO batches (name, revision, updated_at) "
                    "VALUES (?, 1, datetime('now')) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "revision = batches.revision + 1, updated_at = datetime('now')",
                    (name,),
                )
                conn.executemany(
                    "INSERT INTO batch_rows (batch, seq, patient_id, group_name) "
                    "VALUES (?, ?, ?, ?)",
                    [(name, seq, pid, grp) for seq, (pid, grp) in enumerate(rows)],
                )
                row = conn.execute(
                    "SELECT revision FROM batches WHERE name = ?", (name,)
                ).fetchone()
                return int(row["revision"])
        except sqlite3.Error as exc:
            raise DatabaseError(f"批次 {name!r} 保存失败：{exc}") from exc

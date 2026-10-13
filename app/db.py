import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from app.models import Backup, Entry, EntryInput


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    summary TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL,
                    world_id TEXT REFERENCES entries(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS histories (
                    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(entry_id, revision)
                );
                CREATE INDEX IF NOT EXISTS entries_filter ON entries(status, kind, world_id);
            """)

    @staticmethod
    def decode(row):
        result = dict(row)
        result["tags"] = json.loads(result["tags"])
        return result

    def get_in(self, conn, entry_id):
        row = conn.execute("SELECT * FROM entries WHERE id=?", (str(entry_id),)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到資料")
        return self.decode(row)

    def get(self, entry_id):
        with self.connect() as conn:
            return self.get_in(conn, entry_id)

    def list(self, *, kind=None, status=None, q=None, world_id=None, limit=100, offset=0):
        where, args = [], []
        for name, value in [("kind", kind), ("status", status), ("world_id", world_id)]:
            if value is not None:
                where.append(f"{name}=?")
                args.append(str(value))
        if q:
            where.append("instr(lower(title || ' ' || summary || ' ' || content || ' ' || tags), lower(?)) > 0")
            args.append(q)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as conn:
            total = conn.execute("SELECT count(*) FROM entries" + clause, args).fetchone()[0]
            rows = conn.execute("SELECT * FROM entries" + clause + " ORDER BY updated_at DESC, id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return {"items": [self.decode(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def validate_world(self, conn, data, entry_id=None):
        world_id = data.get("world_id")
        if data["kind"] == "world" and world_id:
            raise HTTPException(422, "世界觀不能隸屬另一個世界觀")
        if world_id:
            world = conn.execute("SELECT kind, status FROM entries WHERE id=?", (str(world_id),)).fetchone()
            if not world or world["kind"] != "world":
                raise HTTPException(422, "world_id 必須指向現有世界觀")
            if data["status"] == "published" and world["status"] != "published":
                raise HTTPException(409, "請先發布所屬世界觀")
        if entry_id:
            if data["kind"] != "world" and conn.execute("SELECT 1 FROM entries WHERE world_id=? LIMIT 1", (entry_id,)).fetchone():
                raise HTTPException(409, "仍有條目隸屬此世界觀，不能變更類型")
            if data["status"] == "draft" and conn.execute("SELECT 1 FROM entries WHERE world_id=? AND status='published' LIMIT 1", (entry_id,)).fetchone():
                raise HTTPException(409, "請先將此世界觀的已發布條目改為草稿")

    def context_candidates(self, *, world_id, selected, chars, scenario_id, q, limit):
        where, args = ["status='published'"], []
        if world_id:
            where.append("world_id=?")
            args.append(str(world_id))
        elif selected and not q:
            worlds = list({entry["world_id"] for entry in selected if entry["world_id"]})
            if not worlds:
                return []
            placeholders = ",".join("?" for _ in worlds)
            where.append(f"((kind='world' AND id IN ({placeholders})) OR (kind IN ('lore','rule') AND world_id IN ({placeholders})))")
            args.extend(worlds * 2)
        if chars:
            where.append("kind!='character'")
        if scenario_id:
            where.append("kind!='scenario'")
        if selected:
            where.append("id NOT IN (" + ",".join("?" for _ in selected) + ")")
            args.extend(entry["id"] for entry in selected)
        if q:
            where.append("instr(lower(title || ' ' || summary || ' ' || content || ' ' || tags), lower(?)) > 0")
            args.append(q)
        # Apply relevance in SQL before LIMIT, so older linked worlds cannot disappear
        # behind newer unrelated records. Fetch one extra to signal truncation.
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM entries WHERE " + " AND ".join(where) + " ORDER BY updated_at DESC, id LIMIT ?", [*args, limit + 1]).fetchall()
        return [self.decode(row) for row in rows]

    @staticmethod
    def insert_in(conn, data):
        columns = ["id", "kind", "title", "summary", "content", "tags", "world_id", "status", "revision", "created_at", "updated_at"]
        values = [json.dumps(data[name], ensure_ascii=False) if name == "tags" else data[name] for name in columns]
        conn.execute(f"INSERT INTO entries ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)

    @staticmethod
    def record(conn, data):
        conn.execute("INSERT INTO histories VALUES (?,?,?,?)", (data["id"], data["revision"], json.dumps(data, ensure_ascii=False), data["updated_at"]))

    def create(self, payload: EntryInput):
        stamp = now()
        data = {**payload.model_dump(mode="json"), "id": str(uuid4()), "revision": 1, "created_at": stamp, "updated_at": stamp}
        with self.connect(write=True) as conn:
            self.validate_world(conn, data)
            self.insert_in(conn, data)
            self.record(conn, data)
        return data

    def update_in(self, conn, entry_id, payload, expected_revision):
        old = self.get_in(conn, entry_id)
        if old["revision"] != expected_revision:
            raise HTTPException(409, "資料已被更新，請重新載入後再儲存")
        data = {**payload, "id": entry_id, "revision": old["revision"] + 1, "created_at": old["created_at"], "updated_at": now()}
        self.validate_world(conn, data, entry_id)
        fields = ["kind", "title", "summary", "content", "tags", "world_id", "status", "revision", "updated_at"]
        conn.execute("UPDATE entries SET " + ",".join(f"{name}=?" for name in fields) + " WHERE id=?", [*(json.dumps(data[name], ensure_ascii=False) if name == "tags" else data[name] for name in fields), entry_id])
        self.record(conn, data)
        return data

    def update(self, entry_id, payload, expected_revision):
        with self.connect(write=True) as conn:
            return self.update_in(conn, str(entry_id), payload.model_dump(mode="json"), expected_revision)

    def delete(self, entry_id, expected_revision):
        with self.connect(write=True) as conn:
            old = self.get_in(conn, entry_id)
            if old["revision"] != expected_revision:
                raise HTTPException(409, "資料已被更新，請重新載入")
            if conn.execute("SELECT 1 FROM entries WHERE world_id=? LIMIT 1", (str(entry_id),)).fetchone():
                raise HTTPException(409, "此世界觀仍有相關條目，請先移除或重新指定所屬世界觀")
            conn.execute("DELETE FROM entries WHERE id=?", (str(entry_id),))

    def history(self, entry_id):
        with self.connect() as conn:
            self.get_in(conn, entry_id)
            rows = conn.execute("SELECT * FROM histories WHERE entry_id=? ORDER BY revision DESC", (str(entry_id),)).fetchall()
        return {"items": [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in rows]}

    def restore(self, entry_id, revision, expected_revision):
        with self.connect(write=True) as conn:
            row = conn.execute("SELECT snapshot FROM histories WHERE entry_id=? AND revision=?", (str(entry_id), revision)).fetchone()
            if not row:
                raise HTTPException(404, "找不到歷史版本")
            snapshot = json.loads(row["snapshot"])
            payload = EntryInput.model_validate({key: snapshot[key] for key in EntryInput.model_fields}).model_dump(mode="json")
            return self.update_in(conn, str(entry_id), payload, expected_revision)

    def export(self):
        with self.connect() as conn:
            conn.execute("BEGIN")
            entries = [self.decode(row) for row in conn.execute("SELECT * FROM entries ORDER BY created_at, id")]
            histories = [{**dict(row), "snapshot": json.loads(row["snapshot"])} for row in conn.execute("SELECT * FROM histories ORDER BY entry_id, revision")]
        return {"format": "airp-backup-v1", "exported_at": now(), "entries": entries, "histories": histories}

    def import_backup(self, backup: Backup):
        entries = [entry.model_dump(mode="json") for entry in backup.entries]
        by_id = {entry["id"]: entry for entry in entries}
        if len(by_id) != len(entries):
            raise HTTPException(422, "備份包含重複 ID")
        histories = [item.model_dump(mode="json") for item in backup.histories]
        history_map = {}
        for item in histories:
            snapshot = item["snapshot"]
            key = (item["entry_id"], item["revision"])
            if (
                item["entry_id"] not in by_id
                or snapshot["id"] != item["entry_id"]
                or snapshot["revision"] != item["revision"]
                or item["revision"] > by_id[item["entry_id"]]["revision"]
                or snapshot["created_at"] != by_id[item["entry_id"]]["created_at"]
                or item["created_at"] != snapshot["updated_at"]
                or key in history_map
            ):
                raise HTTPException(422, "歷史版本資料不一致")
            history_map[key] = snapshot
        for entry in entries:
            current = history_map.get((entry["id"], entry["revision"]))
            if current is None:
                if histories:
                    raise HTTPException(422, "備份缺少目前版本的歷史資料")
            elif current != entry:
                raise HTTPException(422, "目前資料與歷史版本不一致")
        with self.connect(write=True) as conn:
            for entry in entries:
                if conn.execute("SELECT 1 FROM entries WHERE id=?", (entry["id"],)).fetchone():
                    raise HTTPException(409, "ID 已存在；匯入不覆蓋現有資料，請使用空白資料庫還原")
            for entry in sorted(entries, key=lambda e: e["kind"] != "world"):
                self.validate_world(conn, entry)
                self.insert_in(conn, entry)
            if histories:
                for item in histories:
                    conn.execute("INSERT INTO histories VALUES (?,?,?,?)", (item["entry_id"], item["revision"], json.dumps(item["snapshot"], ensure_ascii=False), item["created_at"]))
            else:
                for entry in entries:
                    self.record(conn, entry)
        return {"imported": len(entries)}

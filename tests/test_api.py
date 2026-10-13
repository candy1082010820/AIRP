"""User-facing checks for the persistent roleplay library and GPT Actions API."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Entry


ADMIN = {"Authorization": "Bearer admin-test-key"}
READER = {"Authorization": "Bearer read-test-key"}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "library.sqlite3",
        admin_key="admin-test-key",
        read_key="read-test-key",
        public_url="https://example.com",
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as active_client:
        yield active_client


def entry_data(**overrides):
    return {
        "kind": "lore",
        "title": "浮空城的歷史",
        "summary": "角色扮演背景資料",
        "content": "浮空城由七座島嶼組成。",
        "tags": ["奇幻", "浮空城"],
        "world_id": None,
        "status": "published",
        **overrides,
    }


def create_entry(client, **overrides):
    response = client.post("/api/entries", json=entry_data(**overrides), headers=ADMIN)
    assert response.status_code == 201, response.text
    return response.json()


def update_entry(client, entry, **overrides):
    data = {key: entry[key] for key in entry_data()}
    data.update(overrides)
    data["expected_revision"] = entry["revision"]
    return client.put(f"/api/entries/{entry['id']}", json=data, headers=ADMIN)


def history(client, entry_id):
    response = client.get(f"/api/entries/{entry_id}/history", headers=ADMIN)
    assert response.status_code == 200, response.text
    return {item["revision"]: item["snapshot"] for item in response.json()["items"]}


def test_health_and_saved_entries_survive_a_restart(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        saved = create_entry(client)
        assert saved["revision"] == 1
        assert saved["id"]
        assert saved["created_at"]
        assert saved["updated_at"]

    with TestClient(create_app(settings)) as restarted:
        response = restarted.get(f"/api/entries/{saved['id']}", headers=READER)
        assert response.status_code == 200
        assert response.json() == saved
        assert history(restarted, saved["id"])[1] == saved


def test_update_and_delete_require_the_current_revision(client):
    saved = create_entry(client)
    updated = update_entry(client, saved, content="浮空城現已擴增至八座島嶼。")
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2

    stale_update = update_entry(client, saved, content="過時的編輯內容")
    assert stale_update.status_code == 409
    current = client.get(f"/api/entries/{saved['id']}", headers=READER).json()
    assert current["content"] == "浮空城現已擴增至八座島嶼。"
    assert current["revision"] == 2

    stale_delete = client.delete(
        f"/api/entries/{saved['id']}", params={"expected_revision": 1}, headers=ADMIN
    )
    assert stale_delete.status_code == 409
    deleted = client.delete(
        f"/api/entries/{saved['id']}", params={"expected_revision": 2}, headers=ADMIN
    )
    assert deleted.status_code == 204
    assert client.get(f"/api/entries/{saved['id']}", headers=ADMIN).status_code == 404


def test_simultaneous_edits_do_not_silently_overwrite_each_other(client):
    saved = create_entry(client)
    candidates = ["第一位管理員的修訂", "第二位管理員的修訂"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda text: update_entry(client, saved, content=text), candidates))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    current = client.get(f"/api/entries/{saved['id']}", headers=ADMIN).json()
    assert current["content"] == winner["content"]
    assert current["revision"] == 2
    assert set(history(client, saved["id"])) == {1, 2}


def test_read_credentials_show_only_published_entries(client):
    published = create_entry(client, title="公開世界設定")
    draft = create_entry(client, title="秘密草稿", content="秘密劇情尚未發布", status="draft")

    response = client.get("/api/entries", headers=READER)
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [entry["id"] for entry in response.json()["items"]] == [published["id"]]
    assert client.get("/api/entries", params={"status": "draft"}, headers=READER).status_code == 403
    assert client.get(f"/api/entries/{draft['id']}", headers=READER).status_code == 404
    assert client.get(f"/api/entries/{published['id']}", headers=READER).status_code == 200

    draft_list = client.get("/api/entries", params={"status": "draft"}, headers=ADMIN)
    assert draft_list.status_code == 200
    assert [entry["id"] for entry in draft_list.json()["items"]] == [draft["id"]]


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong-key"}])
def test_missing_and_invalid_credentials_are_rejected(client, headers):
    assert client.get("/api/entries", headers=headers).status_code == 401
    assert client.post("/api/entries", json=entry_data(), headers=headers).status_code == 401
    assert client.get("/api/context", headers=headers).status_code == 401


def test_keyless_development_accepts_only_actual_loopback_peers(tmp_path):
    local_settings = Settings(db_path=tmp_path / "local-only.sqlite3")
    app = create_app(local_settings)
    with TestClient(app, client=("127.0.0.1", 12345)) as local:
        created = local.post("/api/entries", json=entry_data())
        assert created.status_code == 201
        assert local.get("/api/entries").json()["total"] == 1

    with TestClient(app) as remote:
        assert remote.get("/health").status_code == 200
        assert remote.get("/api/entries").status_code == 403
        spoofed = remote.get(
            "/api/entries",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        )
        assert spoofed.status_code == 403


@pytest.mark.parametrize(
    "admin_key,read_key",
    [("admin-only", ""), ("", "read-only"), ("same-key", "same-key"), ("", "")],
)
def test_public_deployments_require_two_distinct_credentials(tmp_path, admin_key, read_key):
    with pytest.raises(ValueError):
        Settings(
            db_path=tmp_path / "unsafe-public.sqlite3",
            admin_key=admin_key,
            read_key=read_key,
            public_url="https://example.com",
        )


def test_read_credentials_cannot_mutate_or_download_the_backup(client):
    saved = create_entry(client)
    assert client.get(f"/api/entries/{saved['id']}/history", headers=READER).status_code == 403
    assert client.post("/api/entries", json=entry_data(), headers=READER).status_code == 403
    assert client.put(
        f"/api/entries/{saved['id']}",
        json={**entry_data(), "expected_revision": 1},
        headers=READER,
    ).status_code == 403
    assert client.delete(
        f"/api/entries/{saved['id']}", params={"expected_revision": 1}, headers=READER
    ).status_code == 403
    assert client.post(
        f"/api/entries/{saved['id']}/restore",
        json={"revision": 1, "expected_revision": 1},
        headers=READER,
    ).status_code == 403
    assert client.get("/api/export", headers=READER).status_code == 403


def test_listing_searches_and_filters_published_content(client):
    world = create_entry(client, kind="world", title="群島世界")
    create_entry(client, kind="character", title="莉雅", world_id=world["id"], tags=["旅人"])
    create_entry(client, title="鐘塔傳說", content="每晚九點會有紫色燈光。", world_id=world["id"])
    create_entry(client, title="草稿鐘塔", status="draft")

    search = client.get("/api/entries", params={"q": "紫色燈光"}, headers=READER)
    assert search.status_code == 200
    assert search.json()["total"] == 1
    assert search.json()["items"][0]["title"] == "鐘塔傳說"

    filtered = client.get(
        "/api/entries", params={"kind": "character", "world_id": world["id"]}, headers=READER
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["title"] == "莉雅"

    page = client.get("/api/entries", params={"limit": 1, "offset": 1}, headers=READER).json()
    assert page["total"] == 3
    assert page["limit"] == 1
    assert page["offset"] == 1
    assert len(page["items"]) == 1


def test_history_and_restore_preserve_every_saved_version(client):
    original = create_entry(client)
    edited_response = update_entry(client, original, title="第八島的秘密", content="新版本內容")
    assert edited_response.status_code == 200
    edited = edited_response.json()

    restored_response = client.post(
        f"/api/entries/{original['id']}/restore",
        json={"revision": 1, "expected_revision": edited["revision"]},
        headers=ADMIN,
    )
    assert restored_response.status_code == 200, restored_response.text
    restored = restored_response.json()
    assert restored["revision"] == 3
    assert restored["content"] == original["content"]
    assert restored["title"] == original["title"]
    snapshots = history(client, original["id"])
    assert set(snapshots) == {1, 2, 3}
    assert snapshots[1] == original
    assert snapshots[2] == edited
    assert snapshots[3] == restored

    stale_restore = client.post(
        f"/api/entries/{original['id']}/restore",
        json={"revision": 2, "expected_revision": 2},
        headers=ADMIN,
    )
    assert stale_restore.status_code == 409
    assert history(client, original["id"]) == snapshots


def test_world_links_and_world_deletion_protect_related_entries(client):
    world = create_entry(client, kind="world", title="浮空群島")
    character = create_entry(client, kind="character", title="導航員", world_id=world["id"])
    deletion = client.delete(
        f"/api/entries/{world['id']}", params={"expected_revision": 1}, headers=ADMIN
    )
    assert deletion.status_code == 409
    assert client.get(f"/api/entries/{character['id']}", headers=ADMIN).status_code == 200

    bad_link = client.post(
        "/api/entries", json=entry_data(world_id=character["id"]), headers=ADMIN
    )
    assert bad_link.status_code == 422
    world_with_parent = client.post(
        "/api/entries", json=entry_data(kind="world", world_id=world["id"]), headers=ADMIN
    )
    assert world_with_parent.status_code == 422
    missing_world = client.post(
        "/api/entries",
        json=entry_data(world_id="00000000-0000-0000-0000-000000000000"),
        headers=ADMIN,
    )
    assert missing_world.status_code == 422


def test_published_entries_cannot_reference_a_draft_world(client):
    draft_world = create_entry(client, kind="world", title="建構中的世界", status="draft")
    response = client.post(
        "/api/entries", json=entry_data(world_id=draft_world["id"]), headers=ADMIN
    )
    assert response.status_code == 409

    published_world = create_entry(client, kind="world", title="已發布的世界")
    create_entry(client, world_id=published_world["id"])
    unpublished = update_entry(client, published_world, status="draft")
    assert unpublished.status_code == 409
    assert client.get(f"/api/entries/{published_world['id']}", headers=READER).status_code == 200


def test_gpt_context_reads_latest_selected_world_data_without_drafts(client):
    world = create_entry(client, kind="world", title="島嶼世界", content="WORLD_CONTENT")
    lore = create_entry(client, title="島嶼背景", content="OLD_LORE_CONTENT", world_id=world["id"])
    rule = create_entry(client, kind="rule", title="島嶼規則", content="RULE_CONTENT", world_id=world["id"])
    character_one = create_entry(
        client, kind="character", title="導航員", content="FIRST_CHARACTER_CONTENT", world_id=world["id"]
    )
    character_two = create_entry(
        client, kind="character", title="觀測員", content="SECOND_CHARACTER_CONTENT", world_id=world["id"]
    )
    create_entry(client, kind="character", title="未選角色", content="UNSELECTED_CHARACTER_CONTENT", world_id=world["id"])
    scenario = create_entry(
        client, kind="scenario", title="啟航", content="SCENARIO_CONTENT", world_id=world["id"]
    )
    create_entry(client, kind="scenario", title="未選劇情", content="UNSELECTED_SCENARIO_CONTENT", world_id=world["id"])
    draft = create_entry(client, title="秘密背景", content="SECRET_DRAFT_CONTENT", world_id=world["id"], status="draft")
    other_world = create_entry(client, kind="world", title="其他世界", content="OTHER_WORLD_CONTENT")
    create_entry(client, content="OTHER_LORE_CONTENT", world_id=other_world["id"])
    updated = update_entry(client, lore, content="LATEST_LORE_CONTENT")
    assert updated.status_code == 200

    response = client.get(
        "/api/context",
        params={
            "world_id": world["id"],
            "character_ids": f"{character_one['id']},{character_two['id']}",
            "scenario_id": scenario["id"],
        },
        headers=READER,
    )
    assert response.status_code == 200, response.text
    bundle = response.json()
    included = {entry["id"]: entry for entry in bundle["entries"]}
    assert set(included) == {
        world["id"], lore["id"], rule["id"], character_one["id"], character_two["id"], scenario["id"]
    }
    assert included[lore["id"]]["revision"] == 2
    assert bundle["truncated"] is False
    for text in (
        "WORLD_CONTENT", "LATEST_LORE_CONTENT", "RULE_CONTENT",
        "FIRST_CHARACTER_CONTENT", "SECOND_CHARACTER_CONTENT", "SCENARIO_CONTENT",
    ):
        assert text in bundle["context"]
    for text in (
        "OLD_LORE_CONTENT", "SECRET_DRAFT_CONTENT", "OTHER_WORLD_CONTENT", "OTHER_LORE_CONTENT",
        "UNSELECTED_CHARACTER_CONTENT", "UNSELECTED_SCENARIO_CONTENT",
    ):
        assert text not in bundle["context"]
    explicitly_requested_draft = client.get(
        "/api/context", params={"scenario_id": draft["id"]}, headers=READER
    )
    assert explicitly_requested_draft.status_code == 404


def test_gpt_schema_exposes_only_authenticated_read_actions(client):
    response = client.get("/gpt/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["servers"] == [{"url": "https://example.com"}]
    assert "/api/entries" in schema["paths"]
    assert "/api/context" in schema["paths"]
    assert len(schema["paths"]) == 3
    operation_ids = []
    for path, operations in schema["paths"].items():
        assert set(operations) == {"get"}, path
        operation = operations["get"]
        operation_ids.append(operation["operationId"])
        assert operation.get("security") or schema.get("security")
    assert len(set(operation_ids)) == 3
    schemes = schema["components"]["securitySchemes"].values()
    assert any(scheme.get("type") == "http" and scheme.get("scheme") == "bearer" for scheme in schemes)


def test_gpt_payloads_are_bounded_but_full_entry_content_remains_available(client):
    for index in range(20):
        create_entry(client, title=f"其他設定 {index}")
    full_content = "A" * 50000
    large_world = create_entry(client, kind="world", title="詳盡世界觀", content=full_content)

    listing = client.get("/api/entries", params={"limit": 100}, headers=READER)
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 21
    assert page["limit"] == 20
    assert len(page["items"]) == 20
    assert all("content" not in entry for entry in page["items"])

    response = client.get("/api/context", params={"world_id": large_world["id"]}, headers=READER)
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["truncated"] is True
    assert len(bundle["context"]) <= 30000
    assert "[背景資料超過長度限制；請依來源 ID 呼叫 getEntry 取得完整內容。]" in bundle["context"]
    assert len(response.content) < 32000
    assert [entry["id"] for entry in bundle["entries"]] == [large_world["id"]]
    assert all("content" not in entry for entry in bundle["entries"])

    complete = client.get(f"/api/entries/{large_world['id']}", headers=READER)
    assert complete.status_code == 200
    assert complete.json()["content"] == full_content


def test_large_library_exports_every_record_and_retains_older_linked_context(client):
    def fixture_entry(stamp, **overrides):
        return Entry.model_validate({
            **entry_data(**overrides),
            "id": str(uuid4()),
            "revision": 1,
            "created_at": stamp,
            "updated_at": stamp,
        }).model_dump(mode="json")

    old_stamp = "2025-01-01T00:00:00+00:00"
    new_stamp = "2026-01-01T00:00:00+00:00"
    world = fixture_entry(old_stamp, kind="world", title="舊世界", content="OLD_WORLD_CONTENT")
    character = fixture_entry(
        old_stamp, kind="character", title="舊角色", content="OLD_CHARACTER_CONTENT", world_id=world["id"]
    )
    lore = fixture_entry(old_stamp, title="舊世界背景", content="OLD_LORE_CONTENT", world_id=world["id"])
    records = [world, character, lore]
    records.extend(
        fixture_entry(new_stamp, title=f"較新的無關設定 {index}", content="UNRELATED_LIBRARY_CONTENT")
        for index in range(10000)
    )
    # Bulk fixture preparation avoids thousands of HTTP setup calls; assertions use the API.
    store = client.app.state.store
    with store.connect(write=True) as connection:
        for entry in records:
            store.insert_in(connection, entry)
            store.record(connection, entry)

    exported = client.get("/api/export", headers=ADMIN)
    assert exported.status_code == 200, exported.text[:500]
    backup = exported.json()
    assert len(backup["entries"]) == 10003
    assert len(backup["histories"]) == 10003
    assert {entry["id"] for entry in backup["entries"]} == {entry["id"] for entry in records}

    response = client.get("/api/context", params={"character_ids": character["id"]}, headers=READER)
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert {entry["id"] for entry in bundle["entries"]} == {world["id"], character["id"], lore["id"]}
    assert bundle["truncated"] is False
    for content in ("OLD_WORLD_CONTENT", "OLD_CHARACTER_CONTENT", "OLD_LORE_CONTENT"):
        assert content in bundle["context"]
    assert "UNRELATED_LIBRARY_CONTENT" not in bundle["context"]


def test_backup_roundtrip_preserves_history_and_conflicts_are_atomic(client, tmp_path):
    first = create_entry(client, title="原有資料")
    edited = update_entry(client, first, content="已更新並可匯出的內容")
    assert edited.status_code == 200
    backup_response = client.get("/api/export", headers=ADMIN)
    assert backup_response.status_code == 200
    backup = backup_response.json()
    assert backup["format"] == "airp-backup-v1"
    assert backup["entries"]
    assert backup["histories"]
    destination_settings = Settings(
        db_path=tmp_path / "restored.sqlite3",
        admin_key="admin-test-key",
        read_key="read-test-key",
        public_url="https://example.com",
    )
    with TestClient(create_app(destination_settings)) as destination:
        imported = destination.post("/api/import", json=backup, headers=ADMIN)
        assert imported.status_code == 200, imported.text
        restored = destination.get(f"/api/entries/{first['id']}", headers=ADMIN)
        assert restored.json() == edited.json()
        assert history(destination, first["id"]) == history(client, first["id"])

        new_entry = create_entry(client, title="衝突前不應部分匯入的資料")
        conflict_bundle = client.get("/api/export", headers=ADMIN).json()
        conflict_bundle["entries"].sort(key=lambda entry: entry["id"] != new_entry["id"])
        rejected = destination.post("/api/import", json=conflict_bundle, headers=ADMIN)
        assert rejected.status_code == 409
        assert destination.get(f"/api/entries/{new_entry['id']}", headers=ADMIN).status_code == 404
        assert destination.get("/api/entries", headers=ADMIN).json()["total"] == 1
        assert history(destination, first["id"]) == history(client, first["id"])

    assert client.post("/api/import", json=backup, headers=READER).status_code == 403


def test_import_rejects_invalid_world_references_without_partial_data(client, tmp_path):
    create_entry(client, title="可正常匯入的第一筆資料")
    invalid_entry = create_entry(client, title="含錯誤世界關聯的第二筆資料")
    broken = deepcopy(client.get("/api/export", headers=ADMIN).json())
    missing_world = "00000000-0000-0000-0000-000000000000"
    for entry in broken["entries"]:
        if entry["id"] == invalid_entry["id"]:
            entry["world_id"] = missing_world
    for item in broken["histories"]:
        if item["entry_id"] == invalid_entry["id"]:
            item["snapshot"]["world_id"] = missing_world
    destination_settings = Settings(
        db_path=tmp_path / "invalid-import.sqlite3",
        admin_key="admin-test-key",
        read_key="read-test-key",
        public_url="https://example.com",
    )
    with TestClient(create_app(destination_settings)) as destination:
        response = destination.post("/api/import", json=broken, headers=ADMIN)
        assert response.status_code == 422, response.text
        assert destination.get("/api/entries", headers=ADMIN).json()["total"] == 0


def test_import_rejects_future_history_and_leaves_database_usable(client, tmp_path):
    original = create_entry(client, title="可繼續編輯的備份資料")
    valid_backup = client.get("/api/export", headers=ADMIN).json()
    poisoned_backup = deepcopy(valid_backup)
    future_history = deepcopy(poisoned_backup["histories"][0])
    future_history["revision"] = 2
    future_history["snapshot"]["revision"] = 2
    poisoned_backup["histories"].append(future_history)

    destination_settings = Settings(
        db_path=tmp_path / "future-history.sqlite3",
        admin_key="admin-test-key",
        read_key="read-test-key",
        public_url="https://example.com",
    )
    with TestClient(create_app(destination_settings)) as destination:
        rejected = destination.post("/api/import", json=poisoned_backup, headers=ADMIN)
        assert rejected.status_code == 422, rejected.text
        assert destination.get("/api/entries", headers=ADMIN).json()["total"] == 0

        imported = destination.post("/api/import", json=valid_backup, headers=ADMIN)
        assert imported.status_code == 200, imported.text
        updated = update_entry(destination, original, content="匯入後正常更新至第二版")
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        assert set(history(destination, original["id"])) == {1, 2}

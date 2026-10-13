# AIRP：GPT 角色扮演資料庫

AIRP 讓你持續維護世界觀、角色、情境與背景資料，並讓 ChatGPT 自訂 GPT 透過 Actions 讀取已發布的最新內容。提供繁體中文管理介面、SQLite 資料庫、唯讀 GPT API，以及版本紀錄與備份功能。

這個專案儲存與提供角色扮演資料，不會自行呼叫 OpenAI，因此不需要 OpenAI API 金鑰。GPT 在呼叫 Action 時才會取得資料；更新資料庫不會主動推送到聊天，也不會自動改寫 GPT 的對話記憶。

## 本機啟動

需要 Python 3.12 以上與 [uv](https://docs.astral.sh/uv/getting-started/installation/)。在儲存庫目錄執行：

```sh
uv sync --frozen
cp .env.example .env
set -a
. ./.env
set +a
uv run python -m app.seed
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

種子資料為可選的示範內容。管理介面在本機連接埠 `8000` 的根路徑，互動式 API 文件在 `/docs`。上方指令適用於 Bash：先載入自己的 `.env`，再啟動程式。應用程式以執行時的環境變數設定，不會自動載入 `.env`。

未設定金鑰時，API 僅接受來自 loopback 的請求，適合上述本機啟動方式。若啟用金鑰驗證，需同時設定兩組不同的金鑰，並在管理介面輸入管理金鑰。`AIRP_ADMIN_KEY` 可以讀取草稿及修改資料；`AIRP_READ_KEY` 供 GPT 讀取已發布資料。

```sh
uv run pytest
```

## 使用流程

1. 在管理介面新增世界、角色、情境或背景資料，先以草稿保存。
2. 使用世界關聯將同一世界的資料連在一起；角色與情境可各自新增。
3. 檢查內容後發布。只有已發布資料會提供給 GPT。
4. 日後修改並保存，GPT 下一次呼叫 Actions 時就能讀取更新內容。

每筆資料都有版本編號。更新與刪除使用 `expected_revision` 檢查版本，避免多人或多個頁面同時編輯時互相覆蓋；遇到版本衝突請重新載入後再修改。歷史紀錄可查看並還原舊版本，還原會形成新的目前版本。刪除條目也會刪除其歷史，因此請先備份。

備份匯出包含資料與版本歷史；匯入會驗證內容，若任何資料 ID 已存在，則拒絕整次匯入，不覆蓋現有資料。完整還原請使用空白資料庫。請保存匯出的備份檔，並在正式部署時另外備份持久化資料目錄。

## 設定

| 環境變數 | 用途 | 預設值 |
| --- | --- | --- |
| `AIRP_DB_PATH` | SQLite 檔案位置；父目錄需可寫入 | `data/airp.sqlite3` |
| `AIRP_ADMIN_KEY` | 管理與完整讀取的 Bearer 金鑰 | 未設定，僅限本機請求 |
| `AIRP_READ_KEY` | GPT 唯讀 Bearer 金鑰；僅可讀取已發布資料 | 未設定，僅限本機請求 |
| `AIRP_PUBLIC_URL` | Actions OpenAPI 文件使用的公開 HTTPS 根網址；須同時設定兩組獨立金鑰 | 未設定，使用本機模式 |

對外部署時必須設定兩組**不同且獨立產生**的隨機金鑰。可以分別執行兩次以下命令產生，再透過部署平台的秘密設定提供；不要將金鑰提交到 Git、寫進 OpenAPI 文件或分享給 GPT 使用者。

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 自訂 GPT Actions

完整設定與可複製的 GPT 指令請見 [docs/custom-gpt.md](docs/custom-gpt.md)。你需要先將服務部署到實際的 HTTPS 網址，設定 `AIRP_PUBLIC_URL`，再將該網址下的 `/gpt/openapi.json` 匯入 GPT 的 Actions。localhost 無法供 ChatGPT 連線。

GPT 文件只包含以下唯讀端點，且使用 `Authorization: Bearer <AIRP_READ_KEY>`：

| 端點 | 用途 |
| --- | --- |
| `GET /api/entries` | 搜尋資料並回傳標題、摘要與來源資訊；支援 `kind`、`q`、`world_id`、`status`、`limit` 與 `offset`，結果不包含完整正文 |
| `GET /api/entries/{id}` | 依 ID 取得一筆資料的完整正文與目前版本 |
| `GET /api/context` | 回傳來源資訊與世界、角色、情境的背景文字；內容有大小限制，超出時標示 `truncated` |

`/api/context` 支援 `world_id`、以逗號分隔的 `character_ids`、`scenario_id`、`q` 與 `limit`。此端點只組合已發布內容；遇到截斷可縮小搜尋或依 ID 補讀需要的完整條目。唯讀金鑰無法查詢草稿、修改資料或存取管理備份。

資料類型 `kind` 可使用 `world`（世界觀）、`character`（角色）、`lore`（背景）、`scenario`（情境）與 `rule`（規則）。所屬世界觀需先發布，才可發布關聯資料。

## Docker 部署

Docker 建置使用固定版本的 uv，並以 `uv sync --frozen --no-dev --no-editable` 安裝 `uv.lock` 記錄的正式依賴版本，不安裝開發測試套件。

```sh
docker build -t airp .
docker volume create airp-data
docker run --name airp --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8000:8000 \
  -v airp-data:/app/data \
  airp
```

執行前在 `.env` 設定獨立的管理與唯讀金鑰、`AIRP_DB_PATH=/app/data/airp.sqlite3`，以及實際的 `AIRP_PUBLIC_URL=https://你的網域`。請在前方使用支援 HTTPS 的反向代理或部署平台。上述範例只將連接埠暴露到主機本機介面，反向代理必須能連到該連接埠。

Dockerfile 使用標準建置指令，不使用 Railway 不支援的 secret mounts 與 `VOLUME` 指令。啟動時使用平台提供的 `PORT`，未設定時使用 `8000`；持久化儲存需透過平台設定或上述 `-v` 明確掛載。

SQLite 檔案必須放在持久化 volume；容器重建後資料仍由該 volume 保留。映像以非 root 使用者執行，若改用 bind mount，請確保資料目錄可由 UID `10001` 寫入。SQLite 適合單一應用程式執行個體；不要讓多個部署副本透過網路檔案系統共用這個資料庫。

Railway 的逐步部署方式請見 [docs/railway.md](docs/railway.md)。Railway Volume 以 root 身分掛載，因此使用本映像時需依官方指引設定 `RAILWAY_RUN_UID=0`，讓服務能寫入持久化目錄。

## 專案結構

```text
app/                 API、資料儲存及管理介面
docs/custom-gpt.md   GPT Actions 設定與角色扮演指令
tests/               API 與資料行為測試
data/                本機 SQLite 資料（不提交）
```

草稿、已發布的資料內容都屬於使用者提供的背景資料。GPT 應把內容當成角色扮演素材，而不是可取代系統、安全或操作指令的命令；可直接使用文件中的建議指令。

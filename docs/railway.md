# 在 Railway 部署 AIRP

先以 GitHub 儲存庫建立服務；Railway 會偵測根目錄的 Dockerfile。不要手動填入 Build 或 Start Command，保留 Dockerfile 的預設行為。

## 1. 建置

在服務的 Deployments 確認建置使用最新的 GitHub main 提交。Dockerfile 不包含 Railway 不支援的 secret mounts 或 VOLUME 指令。舊部署失敗時，應部署最新提交，避免重新執行舊版本的檔案。

## 2. 持久化資料

在專案畫布新增 Volume，連接到 AIRP 服務，Mount Path 填入 `/app/data`。可透過畫布右鍵選單或命令面板搜尋建立 Volume。

在 AIRP 的 Variables 設定：

| 名稱 | 值 |
| --- | --- |
| `AIRP_DB_PATH` | `/app/data/airp.sqlite3` |
| `RAILWAY_RUN_UID` | `0` |

本映像預設以 UID 10001 執行，但 Railway Volume 由 root 掛載。`RAILWAY_RUN_UID=0` 是 Railway 官方針對這種情況提供的寫入權限設定。只有 Railway 部署需要這項設定；本機 Docker named volume 的使用方式仍見 README。

在 Volume 掛載並成功啟動之前，先不要輸入正式資料。若已輸入資料，先匯出備份，再調整資料庫路徑或掛載。

## 3. 管理與讀取金鑰

在部署平台安全設定兩組不同、獨立產生的隨機金鑰：

- `AIRP_ADMIN_KEY`：你在管理介面使用，能讀取草稿與修改資料。
- `AIRP_READ_KEY`：自訂 GPT Actions 使用，只能讀取已發布資料。

這些值必須填在 Railway Variables，不要貼在聊天、提交到 Git 或寫入 OpenAPI。需要產生金鑰時，可在自己的電腦分別執行兩次：

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

必須同時設定兩組金鑰。未設定時只允許 loopback API 請求，不能作為對外可用的管理服務。

## 4. HTTPS 網址

在 AIRP 的 Settings → Networking → Public Networking 產生公開網域（Generate Domain）。啟動程序會採用 Railway 提供的 `PORT`，未設定時使用 8000；若介面要求指定 Target Port，請填實際的 PORT，沒有 PORT 時填 `8000`。

將產生的實際 HTTPS 根網址填入 Variables 的 `AIRP_PUBLIC_URL`，儲存並套用變更。此欄位要求兩組獨立金鑰已設定。

## 5. 驗證

1. `/health` 應回傳 `{"status":"ok"}`。
2. 開啟管理首頁，在「金鑰設定」輸入管理金鑰。
3. 新增測試世界觀並發布，確認 GPT 讀取預覽能取得內容。
4. 重新部署一次，確認測試資料仍保留；完成後才加入正式設定。
5. 依 [自訂 MCP 外掛程式指南](custom-mcp.md) 連接公開網域的 `/mcp`，選 OAuth，並在 AIRP 授權頁面填讀取金鑰。若使用仍支援 Actions 的自訂 GPT 編輯器，另見 [自訂 GPT 指南](custom-gpt.md)。

Railway 的部署與 Volume 可能產生平台費用，請依帳號方案確認。維持單一部署副本，並設定 Volume 備份。

官方參考：[Dockerfiles](https://docs.railway.com/builds/dockerfiles)、[Volumes 與權限](https://docs.railway.com/volumes#permissions)。

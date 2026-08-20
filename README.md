# KEEPSILENT Online Stock Monitor

多人線上版商品補貨監控服務。

## 功能

- 使用者註冊 / 登入
- 每個帳號獨立的商品監控清單
- 指定商品網址、尺寸、檢查頻率
- 啟用 / 暫停 / 刪除 / 手動檢查
- Discord Webhook 通知
- Telegram Bot Token + Chat ID 通知
- Discord / Telegram 測試通知
- 通知憑證使用 Fernet 加密後存入資料庫
- PostgreSQL
- Docker
- Railway 部署設定

## 1. 產生必要密鑰

```bash
python - <<'PY'
from cryptography.fernet import Fernet
import secrets
print('SECRET_KEY=' + secrets.token_urlsafe(48))
print('ENCRYPTION_KEY=' + Fernet.generate_key().decode())
PY
```

`SECRET_KEY` 用於登入 cookie 簽章，`ENCRYPTION_KEY` 用於加密 Discord / Telegram 憑證。

## 2. 本機 Docker 啟動

把產生的 `ENCRYPTION_KEY` 與 `SECRET_KEY` 放進 `docker-compose.yml`，再執行：

```bash
docker compose up -d --build
```

瀏覽 `http://localhost:8000`。

## 3. Railway 上線

1. 將此專案推到 GitHub。
2. Railway 建立新 Project，加入 GitHub repo 作為 Web Service。
3. 在同一個 Project 新增 PostgreSQL。
4. Web Service Variables 設定：

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=<長隨機字串>
ENCRYPTION_KEY=<Fernet key>
ALLOW_SIGNUP=true
COOKIE_SECURE=true
MIN_INTERVAL_SECONDS=60
```

5. Web Service 產生 Public Domain。
6. 打開該網址，註冊帳號並新增監控。

Railway 會從 Dockerfile 建置。PostgreSQL 直接使用 Railway 提供的 `DATABASE_URL`。

## 4. Discord 設定

在 Discord 頻道建立 Webhook，將 Webhook URL 貼到「通知設定」。按「傳送測試通知」驗證。

## 5. Telegram 設定

1. 使用 BotFather 建立 bot 並取得 Bot Token。
2. 先對 bot 傳一則訊息。
3. 取得 Chat ID。
4. 將 Bot Token 和 Chat ID 貼進通知設定。
5. 按「傳送測試通知」。

## 庫存判斷

目前針對 KEEPSILENT 商品頁判斷：

- `SOLD OUT`
- `COMING SOON`
- `Add to cart`
- HTML 中 size option/button/label 是否 disabled
- 頁面中可能存在的 variant JSON

狀態從非有貨切換到 `IN_STOCK` 或 `POSSIBLY_IN_STOCK` 時發通知。

## 正式營運前應做的事

這份專案是可部署 MVP。若要讓大量公開使用者註冊，建議再加：

- Email 驗證 / 密碼重設
- CAPTCHA / 註冊 rate limit
- 每人監控商品數量上限
- Worker 與 Web Service 分離
- Redis / queue
- 管理後台
- Terms / Privacy Policy
- 更精準的 KEEPSILENT variant API adapter
- 監控請求節流與 backoff

目前背景監控器跟 Web Service 在同一個 process，因此正式部署請維持單一 Uvicorn worker，避免重複檢查。流量變大後再拆成獨立 worker。

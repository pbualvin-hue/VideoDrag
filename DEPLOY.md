# DEPLOY.md — Pi 部署手冊

> microSD 到、Pi 燒好 64-bit OS 後,照這份走。除了首次 install.sh,
> 之後所有維運都在 PWA 管理頁(零指令原則)。

## 燒錄 microSD(Pi 4 / 4GB / 128GB card,2026-07 定案)

- **OS:Raspberry Pi OS Lite(64-bit)**,目前基底為 Debian 13 Trixie。
  - 64-bit 是 CLAUDE.md 的 Non-Negotiable,且 Docker arm64 + onnxruntime 需要。
  - Lite(無桌面):這是無頭伺服器,全程 PWA+Tailscale 存取,省 4GB RAM。
  - **不要選 32-bit / Desktop / Full**。
- **工具:Raspberry Pi Imager**。選 OS →「Raspberry Pi OS (other)」→
  「Raspberry Pi OS Lite (64-bit)」。
- 燒錄前按齒輪(或 Ctrl+Shift+X)開進階設定,預設好無頭開機:
  - 主機名稱:`vidrag`(之後 Tailscale/Serve 用得到)
  - 啟用 SSH(密碼或金鑰)
  - 使用者名稱/密碼
  - Wi-Fi SSID/密碼 + 國別
  - 語系/時區:Asia/Taipei
  - 設好後燒錄 → 插卡開機即可 SSH,不需接螢幕鍵盤。
- 128GB card 空間充裕;SD 卡耗損靠每日備份與(未來).db 移 USB SSD 緩解。

## 前置(在 Pi 上,一次性)

```bash
# 1. 確認 64-bit OS 與記憶體(Phase 0 第 1 項)
uname -m          # 應為 aarch64
free -h           # <4GB 需先加 swap(見下方「若 <4GB」)

# 2. 取得專案(git clone 或把 data/ + 整個 repo 複製過去)
cd ~/vidrag

# 3. 一鍵安裝(唯一的終端機步驟)
bash install.sh
```

`install.sh` 會:裝 Docker(若無)→ 建 data/.env → `docker compose up -d --build`
(首次會下載相依與 bge 模型,arm64 上約數分鐘)。

## arm64 相依預查(2026-07-07 本機查過 PyPI/GitHub/Docker Hub,風險已排除)

明早 Docker 建置最可能卡在「native 套件在 arm64 + Python 3.14 缺 wheel」——已逐一查過,**全部齊備**:

| 元件 | arm64 狀態 |
|---|---|
| python:3.14-slim | Docker Hub 有 arm64 ✓ |
| deno v2.9.1 | aarch64-unknown-linux-gnu ✓(yt-dlp JS runtime) |
| onnxruntime 1.27 | cp314 manylinux_2_28 aarch64 ✓(最大地雷,已排除) |
| sqlite-vec / tokenizers / fastembed | py3-none / abi3 / 純 python ✓ |
| pillow / numpy / pydantic-core | cp314 aarch64 ✓ |

manylinux_2_28 需 glibc≥2.28;Pi OS Bookworm 是 2.36,滿足。結論:**Docker 建置應順**,若仍失敗多半是網路或映像 tag,看 log 即知。

## 部署後前置驗證(對照 PLAN.md Phase 0)

```bash
# 一鍵煙霧測試:確認所有 native 元件在 arm64 上真的能動
docker compose exec vidrag python scripts/preflight.py
# 全綠代表 ffmpeg/deno/sqlite trigram/sqlite-vec/onnxruntime/opencc 都正常
```


```bash
# Phase 0 第 3 項:Pi 上 yt-dlp 反爬實測
docker compose exec vidrag python -m app.ingest "https://youtu.be/aqz-KE-bpKQ"

# Phase 0 第 4 項:SQLite trigram(容器內)
docker compose exec vidrag python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute(\"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')\"); print('trigram OK')"

# 服務健康
curl -s http://localhost:8080/api/setup/status
```

## Tailscale Serve(PWA HTTPS 前提)

```bash
tailscale serve --bg 8080
tailscale serve status        # 取得 https://<機器名>.<tailnet>.ts.net
```

## iPhone(Phase 0 第 5 項 + Phase 3 入口)

1. 確認 Tailscale app 已開 VPN On Demand(你 07-06 已設定)。
2. 手機開 `https://<機器名>.<tailnet>.ts.net` → 設定精靈填金鑰。
3. 管理頁 →「iOS 捷徑」→ 產生 QR → 依步驟建「存到 vidrag」捷徑。

## Phase 3 Pi 相依驗收(對照 PLAN.md 完成標準)

- [ ] iPhone 外網分享連結 → PWA 看到進度 → 完成後提問成功
- [ ] 模擬離線分享:連結留備忘錄,事後 PWA 手動貼上補攝取
- [ ] 模擬餘額不足:影片卡顯示人話原因(改管理頁把 key 換成無效值測)
- [ ] Pi 重開機:`sudo reboot` 後服務自動恢復、殘留 job 回收
- [ ] 還原:管理頁備份 →〔還原〕→ 通過 integrity_check
- [ ] 搬遷演練:另一機僅用 data/ + docker-compose.yml 還原

## 若 Pi 記憶體 <4GB

```bash
# 加 2GB swap(embedding + Docker 建置需要)
sudo dphys-swapfile swapoff
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile
sudo dphys-swapfile setup && sudo dphys-swapfile swapon
```
若仍吃緊,config 可整體降級為 Haiku(省錢模式)並考慮把 .db 放 USB SSD。

## 部署後才做的事(需外部輸入)

- **IG(Phase 4-5)**:管理頁上傳 IG cookie 後才能攝取 Reels。
- **異地加密備份**:填 data/.env 的 `BACKUP_RCLONE_REMOTE` + `BACKUP_AGE_RECIPIENT`。
- **Phase 5 MCP**:Pi 穩定後,管理頁「連接 Claude Desktop」生成 connector 設定。

## 疑難

- Docker 建置在 arm64 失敗:先看 `docker compose logs`。deno arm64 安裝或
  onnxruntime arm64 wheel 是最可能的卡點——回報 log 我協助。
- yt-dlp 破版:管理頁一鍵更新;全面破版時 `docker compose --profile cobalt up -d`
  啟用 cobalt 備援。

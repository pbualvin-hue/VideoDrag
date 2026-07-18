#!/usr/bin/env bash
# vidrag — the ONLY terminal step in the whole system (UX.md 前提).
# Installs Docker if missing, then builds and starts the container.
# After this runs once, everything else is a button in the PWA.
set -euo pipefail

echo "== vidrag 首次安裝 =="

if ! command -v docker >/dev/null 2>&1; then
  echo "-> 安裝 Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  echo "   (若剛加入 docker 群組,登出再登入一次或用 sudo 重跑本腳本)"
fi

# Ensure the data directory and a starter .env exist.
mkdir -p data
if [ ! -f data/.env ]; then
  cp .env.example data/.env
  echo "-> 已建立 data/.env;可留白,首次開啟 PWA 會有設定精靈引導填入金鑰。"
fi

echo "-> 建置並啟動容器(首次會下載相依套件與 embedding 模型,約數分鐘)..."
docker compose up -d --build

echo ""
echo "== 完成 =="
echo "1. 在同一台機器上執行:  tailscale serve --bg 8080"
echo "   取得 https://<機器名>.<tailnet>.ts.net 網址(PWA 安裝前提)。"
echo "2. iPhone 開啟該網址 -> 設定精靈 -> 產生 iOS 捷徑 QR。"
echo "3. 之後所有維運都在 PWA 管理頁,不需再開終端機。"

// Options page: persist host + APP_TOKEN into chrome.storage.sync.
const $ = (id) => document.getElementById(id);

chrome.storage.sync.get(["host", "token"], ({ host, token }) => {
  if (host) $("host").value = host;      // saved value wins over injected default
  if (token) $("token").value = token;
});

$("save").onclick = () => {
  const host = $("host").value.trim().replace(/\/+$/, "");
  const token = $("token").value.trim();
  if (!host || !token) {
    $("msg").style.color = "#B00020";
    $("msg").textContent = "網址與 token 都要填";
    return;
  }
  chrome.storage.sync.set({ host, token }, () => {
    $("msg").style.color = "#2E7D32";
    $("msg").textContent = "已儲存 ✓ 到任何頁面點工具列圖示或右鍵即可入庫";
  });
};

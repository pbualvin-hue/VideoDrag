// 存到 vidrag — MV3 service worker.
// Sends the current page / selected text / clicked link to the vidrag
// /api/ingest endpoint. Host + APP_TOKEN live in chrome.storage.sync
// (set on the options page); the token is never baked into the extension.

const MENUS = [
  { id: "vidrag-page", title: "存到 vidrag（這個頁面）", contexts: ["page"] },
  { id: "vidrag-sel", title: "存到 vidrag（選取的文字）", contexts: ["selection"] },
  { id: "vidrag-link", title: "存到 vidrag（這個連結）", contexts: ["link"] },
];

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    for (const m of MENUS) chrome.contextMenus.create(m);
  });
}

chrome.runtime.onInstalled.addListener(buildMenus);
chrome.runtime.onStartup.addListener(buildMenus);

async function getConfig() {
  const { host, token } = await chrome.storage.sync.get(["host", "token"]);
  return { host: (host || "").replace(/\/+$/, ""), token: token || "" };
}

// Badge feedback on the toolbar icon (no notifications permission, no icon
// asset needed). The setTimeout clear is best-effort — an MV3 service worker
// may be recycled before it fires — so each ingest() also clears any stale
// badge at the start; the badge therefore always reflects the latest action.
async function flashBadge(text, color) {
  await chrome.action.setBadgeBackgroundColor({ color });
  await chrome.action.setBadgeText({ text });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
}

async function ingest(payload) {
  await chrome.action.setBadgeText({ text: "" });   // clear any stale outcome
  const { host, token } = await getConfig();
  if (!host || !token) {
    chrome.runtime.openOptionsPage();
    await flashBadge("設定", "#B00020");
    return;
  }
  try {
    const resp = await fetch(host + "/api/ingest", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-app-token": token },
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await resp.json(); } catch (_) { /* non-JSON error body */ }
    if (!resp.ok) throw new Error(data.detail || ("HTTP " + resp.status));
    const status = data.results && data.results[0] && data.results[0].status;
    await flashBadge(status === "duplicate" ? "＝" : "✓", "#2E7D32");
  } catch (err) {
    // Surface, don't swallow (error-handling rule): red badge + console.
    await flashBadge("✕", "#B00020");
    console.error("vidrag ingest failed:", err);
  }
}

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === "vidrag-sel" && info.selectionText) {
    ingest({ text: info.selectionText });
  } else if (info.menuItemId === "vidrag-link" && info.linkUrl) {
    ingest({ url: info.linkUrl });
  } else if (info.menuItemId === "vidrag-page" && info.pageUrl) {
    ingest({ url: info.pageUrl });
  }
});

chrome.action.onClicked.addListener((tab) => {
  if (tab && tab.url) ingest({ url: tab.url });
  else chrome.runtime.openOptionsPage();
});

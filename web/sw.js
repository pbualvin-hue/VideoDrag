/* Offline shell only (UX.md: Web Push 選配後補). API calls always go to
 * the network; static shell falls back to cache when offline. */
const CACHE = "vidrag-shell-v2";
const SHELL = ["/", "/index.html", "/app.js", "/manifest.json", "/icon.svg"];

self.addEventListener("install", e => {
  // Prime the shell with cache:"reload" so a deploy's fresh assets bypass the
  // HTTP cache — otherwise a heuristically-cached stale app.js survives the
  // deploy and the client keeps running old code (gap-3 panel stayed hidden).
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(SHELL.map(u => c.add(new Request(u, { cache: "reload" }))))
  ));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // network only
  e.respondWith(
    fetch(e.request)
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});

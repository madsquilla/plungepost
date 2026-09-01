/* PlungePost service worker.
 *
 * The shell (/app) is static, so it is precached and the app opens instantly
 * and offline. Post graphics are cached as they are seen, so posts you have
 * already made stay viewable with no connection. Everything under /api is
 * live-only -- a stale post queue would be worse than an honest error.
 *
 * %CACHE_VERSION% is substituted server-side with a hash of the shell + this
 * file, so shipping a change retires the old cache automatically.
 */

const VERSION = "%CACHE_VERSION%";
const SHELL = "pp-shell-" + VERSION;
const MEDIA = "pp-media-v1";

const SHELL_URLS = [
  "/app",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((cache) => cache.addAll(SHELL_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith("pp-shell-") && k !== SHELL)
            .map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never serve a cached queue or job status.
  if (url.pathname.startsWith("/api/")) return;

  // Navigations: try the network so a new shell lands, fall back to the
  // cached shell when the phone is offline.
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put("/app", copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match("/app", { ignoreSearch: true }))
    );
    return;
  }

  // Post graphics and icons: cache-first, they never change under a given URL.
  if (url.pathname.startsWith("/media/") || url.pathname.startsWith("/icons/")) {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(MEDIA).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }))
    );
    return;
  }

  event.respondWith(fetch(req).catch(() => caches.match(req)));
});

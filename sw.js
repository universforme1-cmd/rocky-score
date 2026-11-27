// Service worker — Rocky Score · DCF · Valeur · Valorisation (appli fusionnée)
// Met en cache uniquement l'enveloppe de l'appli. Les fichiers de données
// (data.json, data-dcf.json, data-valeur.json, data-trident.json) passent toujours par le réseau.
// v4 : nouvel onglet Valorisation (Trident) — force la mise à jour du cache.

const CACHE = "rocky-fusion-v4";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable-512.png"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.endsWith(".json")) return; // données : toujours le réseau
  event.respondWith(
    caches.match(event.request).then(hit => hit || fetch(event.request))
  );
});

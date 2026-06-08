const CACHE = 'tra-v3';
const SHELL = [
  './',
  './index.html',
  './geo.js',
  './manifest.json',
  './icon.svg',
  './data/lines.json',
  './data/stations.json',
  './data/station_of_lines.json',
  './data/shapes.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only cache same-origin requests (skip Firebase, CDN, TDX)
  if (!e.request.url.startsWith(self.location.origin)) return;
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});

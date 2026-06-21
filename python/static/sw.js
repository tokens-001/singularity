// T19: Service Worker — 离线缓存静态资源
const CACHE = 'qidian-v1';
const STATIC = [
  '/',
  '/static/style.css',
  '/static/js/utils.js',
  '/static/js/dashboard.js',
  '/static/js/tasks.js',
  '/static/js/project.js',
  '/static/js/config.js',
  '/static/app.js',
];

// install: 预缓存静态资源
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC).catch(() => {}))
  );
  self.skipWaiting();
});

// activate: 清理旧缓存
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// fetch: 静态资源 cache-first, API network-first
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api/')) return; // 不缓存 API
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});

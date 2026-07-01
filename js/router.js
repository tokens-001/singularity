/**
 * router.js — 基于 hash 的路由模块
 * 解析 URL hash，提取当前路由信息，并通知监听者。
 */

let currentRoute = { page: 'list', params: {} };
let listeners = [];

/**
 * 解析 URL hash 为路由对象。
 * @returns {{ page: string, params: Object }}
 */
function parseHash() {
    const hash = window.location.hash || '#/';
    const match = hash.match(/^#\/article\/(\S+)$/);

    if (match) {
        return { page: 'detail', params: { id: match[1] } };
    }
    return { page: 'list', params: {} };
}

/**
 * 获取当前路由的副本（防止外部直接修改内部数据）。
 * @returns {{ page: string, params: Object }}
 */
export function getRoute() {
    return { page: currentRoute.page, params: { ...currentRoute.params } };
}

/**
 * 编程式导航到指定路由。
 * @param {string} page
 * @param {Object} params
 */
export function navigateTo(page, params = {}) {
    if (page === 'detail' && params.id != null) {
        window.location.hash = `#/article/${params.id}`;
    } else {
        window.location.hash = '#/';
    }
}

/**
 * 注册路由变化监听器，返回取消注册的函数。
 * @param {Function} listener
 * @returns {Function}
 */
export function onRouteChange(listener) {
    listeners.push(listener);
    return () => {
        listeners = listeners.filter(l => l !== listener);
    };
}

/**
 * 初始化路由：同步当前 hash → currentRoute，并监听 hashchange。
 */
export function initRouter() {
    currentRoute = parseHash();

    window.addEventListener('hashchange', () => {
        currentRoute = parseHash();
        listeners.forEach(listener => listener(currentRoute));
    });
}

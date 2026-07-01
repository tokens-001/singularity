/**
 * 路由模块 - 基于 hash 的简单路由
 * 其他模块通过 window.BlogRouter 注册路由和处理导航
 */

const routes = {};

/**
 * 注册路由处理函数
 * @param {string} path - 路由路径模式，如 "/" 或 "/article/:id"
 * @param {Function} handler - 处理函数，接收解析后的参数对象
 */
function registerRoute(path, handler) {
  routes[path] = handler;
}

/**
 * 解析当前 hash 并执行匹配的路由处理函数
 */
function handleRoute() {
  const hash = window.location.hash.slice(1) || "/";

  // 精确匹配
  if (routes[hash]) {
    routes[hash]({});
    return;
  }

  // 参数匹配（如 /article/123）
  for (const pattern in routes) {
    const paramNames = [];
    const regexStr = pattern.replace(/:(\w+)/g, (_, name) => {
      paramNames.push(name);
      return "([^/]+)";
    });
    const regex = new RegExp("^" + regexStr + "$");
    const match = hash.match(regex);
    if (match) {
      const params = {};
      paramNames.forEach((name, i) => {
        params[name] = match[i + 1];
      });
      routes[pattern](params);
      return;
    }
  }

  // 未匹配到任何路由，显示 404
  if (routes["*"]) {
    routes["*"]({});
  }
}

/**
 * 初始化路由监听
 */
function initRouter() {
  window.addEventListener("hashchange", handleRoute);
  handleRoute();
}

// 暴露公共接口
window.BlogRouter = {
  registerRoute,
  initRouter,
  navigate(path) {
    window.location.hash = path;
  }
};

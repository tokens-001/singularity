// 路由模块
// 显式接口：Router类 - addRoute(), init(), getCurrentRoute()

export function Router() {
    this.routes = {};
    this.currentPath = null;
}

Router.prototype.addRoute = function(type, handler) {
    this.routes[type] = handler;
};

Router.prototype.getCurrentRoute = function() {
    var hash = window.location.hash.slice(1) || '/';
    return hash;
};

Router.prototype.parseRoute = function(path) {
    var match = path.match(/^\/article\/(\d+)$/);
    if (match) {
        return { type: 'detail', id: match[1] };
    }
    return { type: 'list' };
};

Router.prototype.handleRoute = function() {
    var currentPath = this.getCurrentRoute();

    if (this.currentPath !== currentPath) {
        this.currentPath = currentPath;
        var route = this.parseRoute(currentPath);
        var handler = this.routes[route.type];
        if (handler) {
            handler(route);
        }
    }
};

Router.prototype.init = function() {
    var self = this;
    window.addEventListener('hashchange', function() { self.handleRoute(); });
    // 首次加载时立即处理
    self.handleRoute();
};

// 简单的路由模块
export class Router {
    constructor() {
        this.routes = new Map();
        this.currentRoute = null;
    }
    
    // 注册路由
    addRoute(path, handler) {
        this.routes.set(path, handler);
    }
    
    // 导航到指定路由
    navigate(path) {
        window.location.hash = path;
    }
    
    // 获取当前路由
    getCurrentRoute() {
        const hash = window.location.hash.slice(1) || '/';
        return hash;
    }
    
    // 解析路由参数
    parseRoute(path) {
        const match = path.match(/^\/article\/(\d+)$/);
        if (match) {
            return { type: 'detail', id: match[1] };
        }
        return { type: 'list' };
    }
    
    // 处理路由变化
    handleRoute() {
        const currentPath = this.getCurrentRoute();
        const route = this.parseRoute(currentPath);
        
        if (this.currentRoute !== currentPath) {
            this.currentRoute = currentPath;
            const handler = this.routes.get(route.type);
            if (handler) {
                handler(route);
            }
        }
    }
    
    // 初始化路由监听
    init() {
        window.addEventListener('hashchange', () => this.handleRoute());
        window.addEventListener('load', () => this.handleRoute());
    }
}

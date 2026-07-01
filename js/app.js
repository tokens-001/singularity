import { Router } from './router.js';
import { renderArticleList } from './list.js';
import { renderArticleDetail } from './detail.js';

// 初始化应用
function initApp() {
    const router = new Router();
    
    // 注册路由
    router.addRoute('list', () => {
        renderArticleList();
    });
    
    router.addRoute('detail', (route) => {
        renderArticleDetail(route.id);
    });
    
    // 启动路由
    router.init();
}

// 启动应用
initApp();

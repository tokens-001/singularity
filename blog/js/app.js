import { Router } from './router.js';
import { renderArticleList, renderArticleDetail } from './views.js';

// 应用挂载点
const APP_CONTAINER = document.getElementById('app');

// 显式接口：将渲染结果写入容器
function mount(html) {
    APP_CONTAINER.innerHTML = html;
}

// 初始化应用
export function initApp() {
    const router = new Router();

    router.addRoute('list', () => {
        mount(renderArticleList());
    });

    router.addRoute('detail', (route) => {
        mount(renderArticleDetail(route.id));
    });

    router.init();
}

// 路由模块
import { renderArticleList } from './articleList.js';
import { renderArticleDetail } from './articleDetail.js';

let currentRoute = '';

export function initRouter() {
    window.addEventListener('hashchange', handleRoute);
    handleRoute();
}

function handleRoute() {
    const hash = window.location.hash || '#/';
    const path = hash.slice(1);
    
    if (path === '/' || path === '') {
        if (currentRoute !== 'list') {
            currentRoute = 'list';
            renderArticleList();
        }
    } else if (path.startsWith('/article/')) {
        const id = path.split('/article/')[1];
        if (currentRoute !== `detail-${id}`) {
            currentRoute = `detail-${id}`;
            renderArticleDetail(id);
        }
    } else {
        renderNotFound();
    }
}

function renderNotFound() {
    const app = document.getElementById('app');
    app.innerHTML = `
        <div class="not-found">
            <h2>404</h2>
            <p>页面未找到</p>
            <a href="#/" class="back-link">返回首页</a>
        </div>
    `;
}

// 文章详情模块
import { getArticleById } from './data.js';

export function renderArticleDetail(id) {
    const article = getArticleById(id);
    const app = document.getElementById('app');
    
    if (!article) {
        app.innerHTML = `
            <a href="#/" class="back-link">← 返回列表</a>
            <div class="not-found">
                <h2>404</h2>
                <p>文章不存在</p>
                <a href="#/" class="back-link">返回首页</a>
            </div>
        `;
        return;
    }
    
    const html = `
        <a href="#/" class="back-link">← 返回列表</a>
        <article class="article-detail">
            <h2>${article.title}</h2>
            <div class="article-meta">${article.date}</div>
            <div class="article-content">
                ${article.content}
            </div>
        </article>
    `;
    
    app.innerHTML = html;
}

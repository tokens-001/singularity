// 文章列表模块
import { getAllArticles } from './data.js';

export function renderArticleList() {
    const startTime = performance.now();
    
    const articles = getAllArticles();
    const app = document.getElementById('app');
    
    const html = `
        <div class="article-list">
            ${articles.map(article => `
                <div class="article-item">
                    <h2 class="article-title">
                        <a href="#/article/${article.id}">${article.title}</a>
                    </h2>
                    <div class="article-meta">${article.date}</div>
                </div>
            `).join('')}
        </div>
    `;
    
    app.innerHTML = html;
    
    const endTime = performance.now();
    const renderTime = endTime - startTime;
    
    if (renderTime > 100) {
        console.warn(`文章列表渲染时间: ${renderTime.toFixed(2)}ms，超过100ms限制`);
    }
}

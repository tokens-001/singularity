// 文章详情渲染模块
// 依赖：data模块（通过显式接口 getArticleById）
import { getArticleById } from './data.js';

export function renderArticleDetail(articleId) {
    var article = getArticleById(articleId);
    var app = document.getElementById('app');

    if (!article) {
        // 文章不存在，显示友好的404提示
        app.innerHTML = '<div class="not-found">' +
            '<h2>404 - 文章未找到</h2>' +
            '<p>抱歉，您访问的文章不存在。</p>' +
            '<a href="#/" class="back-link">&larr; 返回首页</a>' +
            '</div>';
        return;
    }

    var contentParagraphs = article.content.split('\n').map(function(p) {
        if (p.trim() === '') return '';
        return '<p>' + escapeHtml(p) + '</p>';
    }).join('');

    app.innerHTML = '<article class="article-detail">' +
        '<a href="#/" class="back-link">&larr; 返回列表</a>' +
        '<h1>' + escapeHtml(article.title) + '</h1>' +
        '<div class="meta"><time>' + escapeHtml(article.date) + '</time></div>' +
        '<div class="content">' + contentParagraphs + '</div>' +
        '</article>';
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

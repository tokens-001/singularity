// 文章列表渲染模块
// 依赖：data模块（通过显式接口 getAllArticles）
import { getAllArticles } from './data.js';

export function renderArticleList() {
    var startTime = performance.now();

    var articles = getAllArticles();
    var app = document.getElementById('app');

    var html = '<ul class="article-list">';
    for (var i = 0; i < articles.length; i++) {
        var article = articles[i];
        html += '<li class="article-item">';
        html += '<h2><a href="#/article/' + article.id + '">' + escapeHtml(article.title) + '</a></h2>';
        html += '<div class="article-meta"><time>' + escapeHtml(article.date) + '</time></div>';
        html += '<p class="article-excerpt">' + escapeHtml(article.excerpt) + '</p>';
        html += '</li>';
    }
    html += '</ul>';

    app.innerHTML = html;

    var endTime = performance.now();
    console.log('文章列表渲染耗时: ' + (endTime - startTime).toFixed(2) + 'ms');
}

function escapeHtml(str) {
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

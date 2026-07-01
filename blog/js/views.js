// 视图模块 - 负责渲染页面HTML
// 依赖：data模块（通过显式接口）
import { getAllArticles, getArticleById } from './data.js';

// 显式接口：渲染文章列表页
export function renderArticleList() {
  const articles = getAllArticles();
  const items = articles.map(article => `
    <li class="article-item">
      <h2><a href="#/article/${article.id}">${escapeHtml(article.title)}</a></h2>
      <div class="article-meta">
        <time>${escapeHtml(article.date)}</time>
      </div>
      <p class="article-summary">${escapeHtml(article.summary)}</p>
    </li>
  `).join('');

  return `<ul class="article-list">${items}</ul>`;
}

// 显式接口：渲染文章详情页，文章不存在时返回404页面
export function renderArticleDetail(id) {
  const article = getArticleById(id);
  if (!article) {
    return render404();
  }

  return `
    <a href="#/" class="back-link">&larr; 返回列表</a>
    <article class="article-detail">
      <h2>${escapeHtml(article.title)}</h2>
      <div class="article-meta">
        <time>${escapeHtml(article.date)}</time>
      </div>
      <div class="article-content">${escapeHtml(article.content).replace(/\n/g, '<br>')}</div>
    </article>
  `;
}

// 显式接口：渲染404页面
export function render404() {
  return `
    <div class="not-found">
      <h2>404 - 页面未找到</h2>
      <p>抱歉，您访问的文章不存在或已被删除。</p>
      <a href="#/">返回首页</a>
    </div>
  `;
}

// 工具函数：转义HTML防止XSS
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

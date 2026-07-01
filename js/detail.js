/**
 * detail.js — 文章详情页渲染模块
 * 通过 import 从 data 模块获取数据（显式接口）。
 * 当文章不存在时显示友好的 404 提示，而非空白或报错。
 */

import { getArticleById } from './data.js';

/**
 * 渲染文章详情到指定容器
 * @param {HTMLElement} container
 * @param {number} id 文章 ID
 */
export function renderDetail(container, id) {
  const article = getArticleById(id);

  if (!article) {
    renderNotFound(container);
    return;
  }

  const paragraphs = article.content
    .split('\n\n')
    .map((p) => `<p>${escapeHtml(p)}</p>`)
    .join('');

  container.innerHTML = `
    <article class="article-detail">
      <a href="#/" class="back-link">&larr; 返回列表</a>
      <h1>${escapeHtml(article.title)}</h1>
      <div class="article-meta">${escapeHtml(article.date)}</div>
      <div class="article-content">${paragraphs}</div>
    </article>
  `;
}

/**
 * 渲染 404 友好提示
 * @param {HTMLElement} container
 */
function renderNotFound(container) {
  container.innerHTML = `
    <div class="not-found">
      <h1>404</h1>
      <p>抱歉，您访问的文章不存在或已被删除。</p>
      <a href="#/">返回首页</a>
    </div>
  `;
}

/**
 * HTML 转义，防止 XSS
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

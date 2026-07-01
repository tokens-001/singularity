/**
 * list.js — 文章列表页渲染模块
 * 通过 import 从 data 模块获取数据（显式接口），不直接访问其内部。
 */

import { getAllArticles } from './data.js';

/**
 * 渲染文章列表到指定容器
 * @param {HTMLElement} container
 */
export function renderList(container) {
  const articles = getAllArticles();

  const html = `
    <ul class="article-list">
      ${articles
        .map(
          (a) => `
        <li class="article-item">
          <h2><a href="#/article/${a.id}">${escapeHtml(a.title)}</a></h2>
          <div class="article-meta">${escapeHtml(a.date)}</div>
          <p class="article-excerpt">${escapeHtml(a.excerpt)}</p>
        </li>
      `,
        )
        .join('')}
    </ul>
  `;

  container.innerHTML = html;
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

/**
 * listPage.js — 文章列表页渲染模块
 * 负责将文章列表渲染到指定容器，通过显式接口与数据模块通信。
 */

import { getAllArticles } from './data.js';

/**
 * HTML 转义，防止 XSS。
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * 渲染文章列表页到指定容器。
 * 使用 DocumentFragment 批量插入，确保 100 条以内渲染时间 ≤100ms。
 * @param {HTMLElement} container — 渲染目标容器
 */
export function render(container) {
    const articles = getAllArticles();

    const fragment = document.createDocumentFragment();

    // 标题
    const heading = document.createElement('h2');
    heading.textContent = '全部文章';
    heading.className = 'page-heading';
    fragment.appendChild(heading);

    // 列表
    const list = document.createElement('ul');
    list.className = 'article-list';

    articles.forEach(article => {
        const li = document.createElement('li');
        li.className = 'article-card';

        const titleLink = document.createElement('a');
        titleLink.href = `#/article/${article.id}`;
        titleLink.textContent = article.title;

        const titleEl = document.createElement('h3');
        titleEl.className = 'article-card__title';
        titleEl.appendChild(titleLink);

        const metaEl = document.createElement('p');
        metaEl.className = 'article-card__meta';
        metaEl.textContent = `📅 ${article.date}`;

        const summaryEl = document.createElement('p');
        summaryEl.className = 'article-card__summary';
        summaryEl.textContent = article.summary;

        li.appendChild(titleEl);
        li.appendChild(metaEl);
        li.appendChild(summaryEl);
        list.appendChild(li);
    });

    fragment.appendChild(list);

    // 一次性清空并插入，减少重排
    container.innerHTML = '';
    container.appendChild(fragment);
}

/**
 * detailPage.js — 文章详情页渲染模块
 * 负责渲染单篇文章详情，文章不存在时显示友好的 404 提示。
 */

import { getArticleById } from './data.js';

/**
 * 渲染文章 404 提示。
 * @param {HTMLElement} container
 */
function renderNotFound(container) {
    container.innerHTML = '';

    const wrapper = document.createElement('div');
    wrapper.className = 'not-found';

    const code = document.createElement('div');
    code.className = 'not-found__code';
    code.textContent = '404';

    const message = document.createElement('p');
    message.className = 'not-found__message';
    message.textContent = '抱歉，您访问的文章不存在或已被删除。';

    const link = document.createElement('a');
    link.className = 'not-found__link';
    link.href = '#/';
    link.textContent = '← 返回首页';

    wrapper.appendChild(code);
    wrapper.appendChild(message);
    wrapper.appendChild(link);
    container.appendChild(wrapper);
}

/**
 * 渲染文章详情页到指定容器。
 * @param {HTMLElement} container — 渲染目标容器
 * @param {Object} params — 路由参数，需包含 id 字段
 */
export function render(container, params) {
    const id = parseInt(params.id, 10);

    // ID 无效或文章不存在 → 友好 404
    if (isNaN(id)) {
        renderNotFound(container);
        return;
    }

    const article = getArticleById(id);
    if (!article) {
        renderNotFound(container);
        return;
    }

    container.innerHTML = '';

    const wrapper = document.createElement('article');
    wrapper.className = 'article-detail';

    // 返回链接
    const back = document.createElement('a');
    back.href = '#/';
    back.className = 'article-detail__back';
    back.textContent = '← 返回文章列表';

    // 标题
    const title = document.createElement('h1');
    title.className = 'article-detail__title';
    title.textContent = article.title;

    // 元信息
    const meta = document.createElement('div');
    meta.className = 'article-detail__meta';
    meta.textContent = `📅 发布于 ${article.date}`;

    // 正文 — 将 \n\n 分段为 <p>
    const content = document.createElement('div');
    content.className = 'article-detail__content';
    const paragraphs = article.content.split('\n\n');
    paragraphs.forEach(text => {
        const p = document.createElement('p');
        p.textContent = text.trim();
        content.appendChild(p);
    });

    wrapper.appendChild(back);
    wrapper.appendChild(title);
    wrapper.appendChild(meta);
    wrapper.appendChild(content);
    container.appendChild(wrapper);
}

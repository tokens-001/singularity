import { getArticles, getArticleById } from './data.js';
import { navigateTo } from './router.js';

export function renderArticleList(container) {
  const articles = getArticles();
  const startTime = performance.now();

  const header = document.createElement('header');
  header.innerHTML = '<h1>个人博客</h1>';

  const list = document.createElement('ul');
  list.className = 'article-list';

  const fragment = document.createDocumentFragment();
  for (const article of articles) {
    const li = document.createElement('li');
    li.className = 'article-item';
    li.innerHTML = `
      <h2>${escapeHtml(article.title)}</h2>
      <p class="summary">${escapeHtml(article.summary)}</p>
      <div class="meta">${escapeHtml(article.date)} · ${escapeHtml(article.author)}</div>
    `;
    li.addEventListener('click', () => {
      navigateTo('detail', { id: article.id });
    });
    fragment.appendChild(li);
  }
  list.appendChild(fragment);

  container.innerHTML = '';
  container.appendChild(header);
  container.appendChild(list);

  const endTime = performance.now();
  console.log(`列表渲染耗时: ${(endTime - startTime).toFixed(2)}ms`);
}

export function renderArticleDetail(container, id) {
  const article = getArticleById(id);

  if (!article) {
    renderNotFound(container);
    return;
  }

  const wrapper = document.createElement('div');
  wrapper.className = 'container';

  const backBtn = document.createElement('button');
  backBtn.className = 'back-btn';
  backBtn.textContent = '← 返回列表';
  backBtn.addEventListener('click', () => {
    navigateTo('list');
  });

  const detail = document.createElement('article');
  detail.className = 'article-detail';
  detail.innerHTML = `
    <h1>${escapeHtml(article.title)}</h1>
    <div class="meta">${escapeHtml(article.date)} · ${escapeHtml(article.author)}</div>
    <div class="content">${escapeHtml(article.content)}</div>
  `;

  wrapper.appendChild(backBtn);
  wrapper.appendChild(detail);

  container.innerHTML = '';
  container.appendChild(wrapper);
}

export function renderNotFound(container) {
  const notFound = document.createElement('div');
  notFound.className = 'not-found';
  notFound.innerHTML = `
    <h2>404</h2>
    <p>抱歉，您访问的文章不存在。</p>
  `;

  container.innerHTML = '';
  container.appendChild(notFound);
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

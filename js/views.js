import { getPosts, getPostById } from "./data.js";

export function renderPostList(container) {
  const posts = getPosts();
  const t0 = performance.now();

  const html = `
    <ul class="post-list">
      ${posts.map(post => `
        <li class="post-item">
          <h2><a href="#/post/${post.id}">${escapeHtml(post.title)}</a></h2>
          <div class="post-meta">${escapeHtml(post.date)}</div>
          <p class="post-summary">${escapeHtml(post.summary)}</p>
        </li>
      `).join("")}
    </ul>
  `;

  container.innerHTML = html;

  const t1 = performance.now();
  if (t1 - t0 > 100) {
    console.warn("Post list render took >100ms:", t1 - t0);
  }
}

export function renderPostDetail(container, id) {
  const postId = parseInt(id, 10);
  const post = getPostById(postId);

  if (!post) {
    renderNotFound(container);
    return;
  }

  container.innerHTML = `
    <article class="post-detail">
      <h1>${escapeHtml(post.title)}</h1>
      <div class="post-meta">${escapeHtml(post.date)}</div>
      <div class="post-content">${escapeHtml(post.content)}</div>
      <a href="#/" class="back-link">← 返回列表</a>
    </article>
  `;
}

export function renderNotFound(container) {
  container.innerHTML = `
    <div class="not-found">
      <h2>404 - 文章未找到</h2>
      <p>抱歉，您访问的文章不存在。</p>
      <a href="#/">← 返回首页</a>
    </div>
  `;
}

function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

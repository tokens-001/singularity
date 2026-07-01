/**
 * 列表视图模块 - 渲染文章列表
 * 依赖: BlogData, BlogRouter
 */

/**
 * 渲染文章列表到 #app 容器
 */
function renderArticleList() {
  const app = document.getElementById("app");
  const articles = window.BlogData.getAllArticles();
  const startTime = performance.now();

  const html = articles.map(article => `
    <li class="article-item">
      <h2><a href="#/article/${article.id}">${escapeHtml(article.title)}</a></h2>
      <div class="article-meta">${escapeHtml(article.date)}</div>
      <p class="article-summary">${escapeHtml(article.summary)}</p>
    </li>
  `).join("");

  app.innerHTML = `<ul class="article-list">${html}</ul>`;

  const elapsed = performance.now() - startTime;
  console.log(`[list] 渲染 ${articles.length} 篇文章耗时: ${elapsed.toFixed(2)}ms`);
}

/**
 * HTML 转义，防止 XSS
 */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// 注册路由
window.BlogRouter.registerRoute("/", renderArticleList);

// 暴露接口供测试使用
window.BlogList = {
  renderArticleList
};

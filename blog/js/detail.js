/**
 * 详情视图模块 - 渲染文章详情页
 * 依赖: BlogData, BlogRouter
 */

/**
 * 渲染文章详情到 #app 容器
 * @param {Object} params - 路由参数，包含 id
 */
function renderArticleDetail(params) {
  const app = document.getElementById("app");
  const article = window.BlogData.getArticleById(params.id);

  if (!article) {
    renderNotFound(params.id);
    return;
  }

  app.innerHTML = `
    <a href="#/" class="back-link">&larr; 返回列表</a>
    <article class="article-detail">
      <h1>${escapeHtml(article.title)}</h1>
      <div class="article-meta">${escapeHtml(article.date)}</div>
      <div class="article-content">${escapeHtml(article.content).replace(/\n/g, "<br>")}</div>
    </article>
  `;
}

/**
 * 渲染 404 页面
 * @param {*} id - 用户请求的文章 ID
 */
function renderNotFound(id) {
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="not-found">
      <h1>404</h1>
      <p>抱歉，找不到文章「${escapeHtml(String(id))}」</p>
      <a href="#/">返回首页</a>
    </div>
  `;
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
window.BlogRouter.registerRoute("/article/:id", renderArticleDetail);

// 注册通配 404 路由
window.BlogRouter.registerRoute("*", function () {
  const app = document.getElementById("app");
  app.innerHTML = `
    <div class="not-found">
      <h1>404</h1>
      <p>页面不存在</p>
      <a href="#/">返回首页</a>
    </div>
  `;
});

// 暴露接口供测试使用
window.BlogDetail = {
  renderArticleDetail,
  renderNotFound
};

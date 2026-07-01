export function getArticles() {
  return [
    {
      id: 1,
      title: "欢迎使用我的个人博客",
      summary: "这是我的第一篇博客文章，欢迎来到我的个人空间。",
      content: "这是我的第一篇博客文章。在这里，我将分享我的技术心得、生活感悟以及一切感兴趣的话题。希望你能在这里找到有价值的内容。",
      date: "2024-01-15",
      author: "博主"
    },
    {
      id: 2,
      title: "JavaScript 模块化开发实践",
      summary: "探讨如何使用 ES6 模块构建可维护的前端应用。",
      content: "ES6 模块系统为 JavaScript 提供了原生的模块化支持。通过 import 和 export 关键字，我们可以清晰地定义模块间的依赖关系，避免全局命名空间污染。",
      date: "2024-01-20",
      author: "博主"
    },
    {
      id: 3,
      title: "CSS 布局技巧分享",
      summary: "Flexbox 和 Grid 布局的实用技巧。",
      content: "现代 CSS 提供了强大的布局工具。Flexbox 适合一维布局，而 Grid 则擅长二维布局。掌握这两者，可以轻松应对大多数页面布局需求。",
      date: "2024-01-25",
      author: "博主"
    }
  ];
}

export function getArticleById(id) {
  const articles = getArticles();
  return articles.find(article => article.id === id) || null;
}

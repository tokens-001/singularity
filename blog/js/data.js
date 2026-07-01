/**
 * 数据模块 - 提供文章数据的访问接口
 * 其他模块通过 window.BlogData 访问数据
 */

const articles = [
  {
    id: 1,
    title: "开始写博客",
    date: "2024-01-15",
    summary: "这是我的第一篇博客文章，记录搭建博客的过程。",
    content: "这是第一篇博客文章的完整内容。\n\n搭建这个博客的初衷很简单：记录学习过程中的思考和实践。\n\n希望这里能成为我技术成长的一个见证。"
  },
  {
    id: 2,
    title: "学习 JavaScript 模块化",
    date: "2024-02-10",
    summary: "ES Module 让前端代码组织更清晰，本文记录学习心得。",
    content: "JavaScript 模块化是现代前端开发的基础。\n\nES Module 使用 import/export 语法，让模块之间的依赖关系显式化。\n\n每个模块有自己的作用域，不会污染全局命名空间。"
  },
  {
    id: 3,
    title: "CSS Flexbox 布局指南",
    date: "2024-03-05",
    summary: "Flexbox 是一维布局的利器，掌握它能大幅提升布局效率。",
    content: "Flexbox 布局模型让容器内的元素排列变得简单直观。\n\n核心概念包括：主轴与交叉轴、flex容器与flex项目。\n\n常用的属性有 justify-content、align-items、flex-direction 等。"
  }
];

/**
 * 获取所有文章（返回列表页用）
 * @returns {Array} 文章列表的副本
 */
function getAllArticles() {
  return articles.map(a => ({ ...a }));
}

/**
 * 根据 ID 获取单篇文章（返回详情页用）
 * @param {number} id - 文章 ID
 * @returns {Object|null} 文章对象或 null
 */
function getArticleById(id) {
  const article = articles.find(a => a.id === Number(id));
  return article ? { ...article } : null;
}

// 暴露公共接口
window.BlogData = {
  getAllArticles,
  getArticleById
};

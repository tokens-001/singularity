/**
 * data.js - 文章数据模块
 * 通过显式接口暴露文章数据，禁止其他模块直接访问内部数组
 */

const articles = [
  {
    id: 1,
    title: '欢迎来到我的博客',
    date: '2024-01-15',
    author: '博主',
    excerpt: '这是我的第一篇博客文章，在这里我将分享我的技术见解和生活感悟。',
    content: '这是我的第一篇博客文章，在这里我将分享我的技术见解和生活感悟。希望这里的内容能对你有所帮助，也欢迎随时交流讨论。'
  },
  {
    id: 2,
    title: 'JavaScript ES6 新特性总结',
    date: '2024-02-10',
    author: '博主',
    excerpt: 'ES6 带来了许多令人兴奋的新特性，包括箭头函数、模板字符串、解构赋值等。',
    content: 'ES6 带来了许多令人兴奋的新特性，包括箭头函数、模板字符串、解构赋值、Promise、async/await 等。这些特性极大地改善了 JavaScript 的开发体验，让代码更加简洁和可读。'
  },
  {
    id: 3,
    title: 'CSS Flexbox 布局指南',
    date: '2024-03-05',
    author: '博主',
    excerpt: 'Flexbox 是现代 CSS 布局的核心工具之一，本文将详细介绍其用法。',
    content: 'Flexbox 是现代 CSS 布局的核心工具之一。它提供了一种灵活的方式来排列、对齐和分配容器内元素的空间，即使元素的大小未知或是动态的。本文详细介绍了 flex-direction、justify-content、align-items 等核心属性。'
  },
  {
    id: 4,
    title: 'Node.js 入门教程',
    date: '2024-04-20',
    author: '博主',
    excerpt: 'Node.js 让 JavaScript 可以在服务端运行，开启了全栈开发的新篇章。',
    content: 'Node.js 让 JavaScript 可以在服务端运行，开启了全栈开发的新篇章。本文将从环境搭建、模块系统、异步编程等基础概念入手，帮助你快速上手 Node.js 开发。'
  },
  {
    id: 5,
    title: 'Git 版本控制实战技巧',
    date: '2024-05-18',
    author: '博主',
    excerpt: '掌握 Git 是每个开发者的必备技能，本文分享一些实用的 Git 命令和工作流。',
    content: '掌握 Git 是每个开发者的必备技能。本文分享了包括 git rebase、git stash、cherry-pick 等高级命令的实战用法，以及如何利用 Git 分支策略来提升团队协作效率。'
  }
];

/**
 * 获取所有文章列表
 * @returns {Array} 文章数组的拷贝
 */
export function getArticles() {
  return articles.map(a => ({ ...a }));
}

/**
 * 根据 ID 获取单篇文章
 * @param {number} id 文章ID
 * @returns {Object|null} 文章对象或 null
 */
export function getArticleById(id) {
  const article = articles.find(a => a.id === Number(id));
  return article ? { ...article } : null;
}

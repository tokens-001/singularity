// 模拟文章数据模块
// 显式接口：getAllArticles(), getArticleById(id)

const articles = [
    {
        id: 1,
        title: '第一篇博客文章',
        date: '2024-01-15',
        excerpt: '这是我的第一篇博客文章，欢迎来到我的个人博客！',
        content: '这是我的第一篇博客文章，欢迎来到我的个人博客！\n\n在这里，我将分享我的技术学习心得、生活感悟以及各种有趣的想法。\n\n希望通过这个平台，能够记录成长的足迹，也希望能与志同道合的朋友交流。'
    },
    {
        id: 2,
        title: '学习JavaScript的心得',
        date: '2024-01-20',
        excerpt: 'JavaScript是一门强大而灵活的编程语言，今天分享一下我的学习心得。',
        content: 'JavaScript是一门强大而灵活的编程语言，今天分享一下我的学习心得。\n\n首先，JavaScript是Web开发的核心语言，掌握它对前端开发者来说至关重要。\n\n其次，JavaScript的生态系统非常丰富，有大量的框架和库可以使用，如React、Vue、Angular等。\n\n最后，持续练习和项目实践是学习编程的最好方式。'
    },
    {
        id: 3,
        title: 'CSS布局技巧分享',
        date: '2024-01-25',
        excerpt: 'CSS布局是前端开发的基础，本文分享一些常用的布局技巧。',
        content: 'CSS布局是前端开发的基础，本文分享一些常用的布局技巧。\n\n1. Flexbox布局：适用于一维布局，可以轻松实现居中、等分布局等效果。\n\n2. Grid布局：适用于二维布局，可以创建复杂的网格系统。\n\n3. 响应式设计：使用媒体查询和相对单位，让页面适配不同设备。\n\n4. 盒模型理解：深入理解content、padding、border、margin的关系。'
    },
    {
        id: 4,
        title: 'HTML语义化的重要性',
        date: '2024-02-01',
        excerpt: '语义化HTML不仅有助于SEO，还能提升代码的可读性和可维护性。',
        content: '语义化HTML不仅有助于SEO，还能提升代码的可读性和可维护性。\n\n什么是语义化？\n语义化就是使用恰当HTML标签来表达内容的含义。\n\n语义化的好处：\n1. 提升可访问性：屏幕阅读器能更好地理解页面结构\n2. 有利于SEO：搜索引擎能更准确地理解页面内容\n3. 代码更易维护：开发者能快速理解代码结构\n4. 便于团队协作：统一的语义标准'
    },
    {
        id: 5,
        title: '前端性能优化实践',
        date: '2024-02-10',
        excerpt: '网站性能直接影响用户体验，本文总结了一些前端性能优化的实践方法。',
        content: '网站性能直接影响用户体验，本文总结了一些前端性能优化的实践方法。\n\n1. 资源优化\n   - 压缩CSS、JavaScript文件\n   - 优化图片大小和格式\n   - 使用CDN加速\n\n2. 加载优化\n   - 延迟加载非关键资源\n   - 使用异步加载脚本\n   - 预加载关键资源\n\n3. 渲染优化\n   - 减少重排重绘\n   - 使用CSS3动画代替JavaScript动画\n   - 虚拟列表处理大数据量\n\n4. 缓存策略\n   - 合理使用浏览器缓存\n   - 使用Service Worker'
    }
];

// 显式接口：获取所有文章（返回副本，防止外部修改内部数据）
export function getAllArticles() {
    return articles.map(function(a) { return Object.assign({}, a); });
}

// 显式接口：根据ID获取单篇文章，不存在返回null
export function getArticleById(id) {
    var articleId = parseInt(id, 10);
    var found = articles.find(function(a) { return a.id === articleId; });
    return found ? Object.assign({}, found) : null;
}

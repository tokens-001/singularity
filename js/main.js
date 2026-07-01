/**
 * main.js — 应用入口模块
 * 获取 #app 容器，启动路由系统。
 */

import { startRouter } from './router.js';

const app = document.getElementById('app');

if (!app) {
  throw new Error('找不到 #app 容器，请检查 index.html');
}

startRouter(app);

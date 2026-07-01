import { getRoute, onRouteChange } from './router.js';
import { renderArticleList, renderArticleDetail } from './views.js';

const app = document.getElementById('app');

function render() {
  const route = getRoute();

  if (route.page === 'detail') {
    renderArticleDetail(app, route.params.id);
  } else {
    renderArticleList(app);
  }
}

onRouteChange(render);
render();

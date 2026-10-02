import { store, type Section } from './state';

const sections: Section[] = ['receivers', 'devices', 'topology', 'settings', 'debug', 'account', 'airplay2'];
function readRoute() {
  const [section, did] = location.hash.slice(1).split('/');
  if (!sections.includes(section as Section)) return null;
  try { return { activeSection: section as Section, tuningDid: did ? decodeURIComponent(did) : null }; }
  catch { return null; }
}
function routeKey(section: Section, did: string | null) {
  return `#${section}${did ? '/' + encodeURIComponent(did) : ''}`;
}

/** UI navigation is observable even when initiated outside the main view. */
export function bindRoutes(render: () => void) {
  let readingHistory = false;
  const initial = readRoute();
  if (initial) store.setUi(initial);
  else history.replaceState(null, '', routeKey(store.get().ui.activeSection, null));
  store.subscribe((state, prev) => {
    if (state.ui.activeSection === prev.ui.activeSection && state.ui.tuningDid === prev.ui.tuningDid) return;
    if (!readingHistory) history.pushState(null, '', routeKey(state.ui.activeSection, state.ui.tuningDid));
    queueMicrotask(render);
  });
  window.addEventListener('popstate', () => {
    const route = readRoute();
    if (!route) return;
    readingHistory = true;
    store.setUi(route);
    readingHistory = false;
  });
}

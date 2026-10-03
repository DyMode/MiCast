import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';

for (const [device, viewport] of [['desktop', {width:1280,height:900}], ['mobile', {width:390,height:844}]]) {
  test(`AirPlay 2 onboarding reports save/start failures and can continue on ${device}`, async t => {
    const {page} = await open(t, {viewport}, {'/api/config': async () => ({...config, airplay2_available:true})});
    await page.evaluate(async () => {
      const {store} = await import('/app/micast/src/state.ts');
      const {api} = await import('/app/micast/src/api.ts');
      window.__airplayStage = 0;
      api.saveAirPlay2Instance = async () => {
        if (window.__airplayStage === 0) throw new Error('播放入口保存失败');
        return {};
      };
      api.setAirPlay2Enabled = async enabled => {
        if (enabled && window.__airplayStage === 1) throw new Error('AirPlay 2 时钟端口被占用');
        return {airplay2_enabled:enabled};
      };
      store.set({access:{...store.get().access,setup_complete:false},onboardingStep:'airplay2'});
      window.dispatchEvent(new Event('micast:request-render'));
    });
    const form = page.locator('[data-setup-airplay2]');
    await form.locator('input[value="true"]').check();
    await form.getByRole('button',{name:'继续',exact:true}).click();
    await form.locator('[data-setup-error]').getByText(/播放入口保存失败/).waitFor();
    assert.equal(await form.getByRole('button',{name:'继续',exact:true}).isEnabled(),true);
    await page.evaluate(()=>window.__airplayStage=1);
    await form.getByRole('button',{name:'继续',exact:true}).click();
    await form.locator('[data-setup-error]').getByText(/时钟端口被占用/).waitFor();

    await form.locator('input[value="false"]').check();
    await form.getByRole('button',{name:'继续',exact:true}).click();
    await page.getByRole('heading',{name:'已经准备好了'}).waitFor();
  });
}
import { existsSync } from 'node:fs';
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { installFixtures, playback, status, config, devices } from './fixtures.mjs';

let server, browser, url;
before(async () => {
  server = await createServer({ server: { host: '127.0.0.1', port: 0 }, logLevel: 'error' });
  await server.listen();
  url = `http://127.0.0.1:${server.httpServer.address().port}/app/micast/`;
  const edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_EXECUTABLE_PATH } : existsSync(edge) ? { executablePath: edge } : {}) });
});
after(async () => { await browser?.close(); await server?.close(); });

async function open(t, options = {}, overrides = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, ...options });
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  t.after(() => assert.deepEqual(errors, [], 'no uncaught browser errors'));
  const requests = await installFixtures(page, overrides);
  await page.goto(url);
  await page.waitForSelector('.app-shell');
  await page.waitForFunction(() => document.querySelector('.now-playing'));
  return { page, requests };
}
async function navigate(page, section) {
  await page.locator(`[data-section="${section}"]:visible`).click();
  await page.waitForFunction(s => document.querySelector('.main-content').dataset.activeSection === s, section);
}

async function openLoggedOutAccount(page) {
  await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    store.set({xiaomi:{logged_in:false,user_id:null}});
    store.setUi({activeSection:'account'});
    window.dispatchEvent(new Event('micast:request-render'));
  });
  await page.waitForSelector('#btn-qr-login');
}

test('closed QR attempts cannot reopen the sheet on late success or failure', async t => {
  const {page} = await open(t);
  await openLoggedOutAccount(page);
  await page.evaluate(async () => {
    const {api} = await import('/app/micast/src/api.ts');
    window.__qrPending = [];
    api.startQRLogin = () => new Promise((resolve,reject)=>window.__qrPending.push({resolve,reject}));
  });
  for (const fail of [false,true]) {
    await page.locator('#btn-qr-login').click();
    await page.waitForSelector('.sheet');
    await page.keyboard.press('Escape');
    await page.evaluate(fail => {
      const pending = window.__qrPending.shift();
      if (fail) pending.reject(new Error('late failure'));
      else pending.resolve({qr_url:'https://example.test/qr',scan_token:'old'});
    },fail);
    await page.waitForTimeout(50);
    assert.equal(await page.locator('.sheet').count(),0);
    assert.equal(await page.evaluate(async ()=>(await import('/app/micast/src/state.ts')).store.get().qr.open),false);
  }
  await page.locator('#btn-qr-login').click();
  await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    store.set({xiaomi:{logged_in:true,user_id:'current-account'}});
    window.__qrPending.shift().resolve({qr_url:'https://example.test/qr',scan_token:'retired-account'});
    window.dispatchEvent(new Event('micast:request-render'));
  });
  await page.waitForTimeout(50);
  assert.equal(await page.locator('.sheet').count(),0);
  assert.equal(await page.evaluate(async()=>(await import('/app/micast/src/state.ts')).store.get().xiaomi.user_id),'current-account');
});

test('an old QR poll cannot confirm or expire a newer login attempt', async t => {
  const {page} = await open(t);
  await openLoggedOutAccount(page);
  await page.evaluate(async () => {
    const {api} = await import('/app/micast/src/api.ts');
    let attempt=0;
    window.__qrPolls=[];
    window.__loginStatusReads=0;
    api.startQRLogin=async()=>({qr_url:'https://example.test/qr',scan_token:`attempt-${++attempt}`});
    api.pollQRLogin=token=>new Promise(resolve=>window.__qrPolls.push({token,resolve}));
    api.getXiaomiStatus=async()=>{window.__loginStatusReads++;return {logged_in:true,user_id:'unexpected'};};
  });
  await page.locator('#btn-qr-login').click();
  await page.waitForFunction(()=>window.__qrPolls.length===1);
  await page.keyboard.press('Escape');
  await page.locator('#btn-qr-login').click();
  await page.waitForFunction(()=>window.__qrPolls.length===2);
  await page.evaluate(()=>window.__qrPolls[0].resolve({status:'confirmed'}));
  await page.waitForTimeout(1300);
  const result=await page.evaluate(async()=>{
    const state=(await import('/app/micast/src/state.ts')).store.get();
    return [state.qr.open,state.qr.scanToken,state.qr.state,window.__loginStatusReads,state.xiaomi.user_id];
  });
  assert.deepEqual(result,[true,'attempt-2','waiting',0,null]);
  await page.keyboard.press('Escape');
  await page.evaluate(()=>window.__qrPolls[1].resolve({status:'expired'}));
  await page.waitForTimeout(50);
  assert.equal(await page.locator('.sheet').count(),0);
});

test('closing a confirmed QR cancels its deferred account refresh', async t => {
  const {page}=await open(t);
  await openLoggedOutAccount(page);
  await page.evaluate(async()=>{
    const {api}=await import('/app/micast/src/api.ts');
    window.__qrStore=(await import('/app/micast/src/state.ts')).store;
    window.__loginStatusReads=0;
    api.startQRLogin=async()=>({qr_url:'https://example.test/qr',scan_token:'confirmed'});
    api.pollQRLogin=async()=>({status:'confirmed'});
    api.getXiaomiStatus=async()=>{window.__loginStatusReads++;return {logged_in:true,user_id:'old'};};
  });
  await page.locator('#btn-qr-login').click();
  await page.waitForFunction(()=>window.__qrStore.get().qr.state==='confirmed');
  await page.keyboard.press('Escape');
  await page.waitForTimeout(1300);
  assert.equal(await page.evaluate(()=>window.__loginStatusReads),0);
});

test('fullscreen and bottom player update controls when target capabilities change', async t => {
  const {page}=await open(t);
  await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    window.__playerBase={status:store.get().status,playback:store.get().playback};
    const {openPlayerFullscreen}=await import('/app/micast/src/player/controller.ts');
    openPlayerFullscreen();
  });
  for (const [revision,enabled] of [[1,true],[2,false],[3,true]]) {
    await page.evaluate(async({revision,enabled})=>{
      const {store}=await import('/app/micast/src/state.ts');
      const {status,playback}=window.__playerBase;
      const runtime={epoch:'capability-change',revision,sequence:revision,sessions:[],targets:[{target:'speaker:d1',owner:'r1',generation:1,capabilities:{volume_control:enabled}}]};
      store.set({status:{...status,runtime},playback:{...playback,runtime,devices:[playback.devices[0]]}});
      document.dispatchEvent(new Event('micast:render-playback'));
    },{revision,enabled});
    assert.equal(await page.locator('#player-slot [data-device-slider="d1"]').isDisabled(),!enabled);
    assert.equal(await page.locator('#player-slot [data-mute="d1"]').isDisabled(),!enabled);
    assert.equal(await page.locator('#playback-slot [data-master-slider]').isDisabled(),!enabled);
  }
});

test('topology-first epoch retires old owners and rejects mixed old projections', async t => {
  const {page}=await open(t);
  const result=await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    const {targetOwner}=await import('/app/micast/src/selectors.ts');
    const status=store.get().status,playback=store.get().playback;
    const runtime=(epoch,revision)=>({epoch,revision,sequence:revision,sessions:[],targets:[{target:'speaker:d1',owner:epoch,generation:1}]});
    store.set({status:{...status,runtime:runtime('old',99)},playback:{...playback,runtime:runtime('old',99)}});
    const accepted=store.acceptRuntime(runtime('new',1),'topology');
    const cleared=store.get().status===null&&store.get().playback===null;
    const staleOwner=targetOwner({status:{...status,runtime:runtime('old',99)},playback:{...playback,runtime:runtime('old',99)}},'d1');
    store.set({status:{...status,runtime:runtime('new',2)},playback:{...playback,runtime:runtime('old',100)}});
    return [accepted,cleared,staleOwner??null,store.get().playback,targetOwner(store.get(),'d1').owner];
  });
  assert.deepEqual(result,[true,true,null,null,'new']);
});

test('late account status and onboarding results cannot restore a logged-out identity', async t => {
  const {page}=await open(t);
  await openLoggedOutAccount(page);
  await page.evaluate(async()=>{
    const {api}=await import('/app/micast/src/api.ts');
    api.loginWithCookie=async()=>({ok:true});
    api.getXiaomiStatus=()=>new Promise(resolve=>{window.__oldAccountStatus=resolve;});
  });
  await page.locator('#btn-cookie-login').click();
  await page.locator('#cookie-user-id').fill('test-user');
  await page.locator('#cookie-pass-token').fill('test-token');
  await page.locator('#btn-cookie-submit').click();
  await page.waitForFunction(()=>Boolean(window.__oldAccountStatus));
  await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    store.invalidateAccountReads();
    store.set({xiaomi:{logged_in:false,user_id:null},devices:[]});
    window.__oldAccountStatus({logged_in:true,user_id:'retired-user'});
  });
  await page.waitForTimeout(50);
  assert.deepEqual(await page.evaluate(async()=>{
    const state=(await import('/app/micast/src/state.ts')).store.get();
    return [state.xiaomi.user_id,state.devices.length];
  }),[null,0]);
});

test('onboarding cannot advance after account change while its device read is pending', async t => {
  const context=await browser.newContext();
  t.after(()=>context.close());
  const page=await context.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let release,entered;
  const started=new Promise(resolve=>{entered=resolve;});
  const pending=new Promise(resolve=>{release=resolve;});
  await installFixtures(page,{
    '/api/access/status':async()=>({access_configured:true,setup_complete:false,authenticated:true,auth_enabled:false}),
    '/api/devices':async()=>{entered();await pending;return [{did:'old-device',name:'旧账号音箱'}];},
  });
  await page.goto(url);
  await Promise.race([started,new Promise((_,reject)=>{
    const timer=setTimeout(()=>reject(new Error('onboarding device request did not start')),7000);
    timer.unref();
  })]);
  await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    store.invalidateAccountReads();
    store.set({xiaomi:{logged_in:false,user_id:null},devices:[],onboardingStep:'access'});
    window.dispatchEvent(new Event('micast:request-render'));
  });
  release();
  await page.waitForTimeout(100);
  const result=await page.evaluate(async()=>{
    const state=(await import('/app/micast/src/state.ts')).store.get();
    return [state.onboardingStep,state.xiaomi.logged_in,state.devices.length,state.fullConfig];
  });
  assert.deepEqual(result,['access',false,0,null]);
  assert.deepEqual(errors,[]);
});

test('narrow navigation works with mouse, touch, and browser back', async t => {
  const { page } = await open(t, { isMobile: true, hasTouch: true });
  await navigate(page, 'receivers');
  await page.locator('[data-section="devices"]:visible').tap();
  await page.waitForFunction(() => document.querySelector('.page-title')?.textContent === '音箱');
  assert.ok(page.url().endsWith('#devices'));
  await page.goBack();
  await page.waitForFunction(() => document.querySelector('.page-title')?.textContent === '播放');
});

test('fullscreen tune mounts the target page; remote stop closes the overlay', async t => {
  const { page } = await open(t);
  await navigate(page, 'devices');
  await page.evaluate(async () => (await import('/app/micast/src/player/controller.ts')).openPlayerFullscreen());
  await page.locator('[data-player-tune]').click();
  await page.locator('[data-player-tune-did]').first().click();
  await page.waitForSelector('[data-tuning-canvas]');
  assert.ok(page.url().endsWith('#devices/d1'));
  await page.goBack();
  await page.waitForSelector('.device-card');
  await page.evaluate(async () => (await import('/app/micast/src/player/controller.ts')).openPlayerFullscreen());
  await page.evaluate(data => window.__stateSocket.onmessage({ data: JSON.stringify({ type: 'playback', data }) }), { ...playback, playing: false, paused: false, devices: [] });
  await page.waitForFunction(() => !document.querySelector('.player-overlay') && !document.querySelector('.now-playing'));
});

test('startup failure exposes an actionable retry', async t => {
  const context = await browser.newContext(); t.after(() => context.close());
  const page = await context.newPage(); let fail = true;
  await installFixtures(page, { '/api/access/status': async route => {
    if (fail) { await route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"服务暂时不可用"}' }); return; }
    return { access_configured: true, setup_complete: true, authenticated: true, auth_enabled: false };
  } });
  await page.goto(url); await page.waitForSelector('[data-boot-retry]');
  assert.match(await page.locator('.boot-content').textContent(), /暂时无法连接/);
  fail = false; await page.locator('[data-boot-retry]').click(); await page.waitForSelector('.sidebar');
});

test('QR sheet owns keyboard focus and Escape restores navigation', async t => {
  const { page } = await open(t);
  await navigate(page, 'settings');
  await page.evaluate(async () => {
    const { store } = await import('/app/micast/src/state.ts');
    store.set({ qr: { open: true, qrUrl: null, scanToken: null, state: 'waiting' } });
    window.dispatchEvent(new Event('micast:request-render'));
  });
  assert.equal(await page.evaluate(() => document.querySelector('.sheet').contains(document.activeElement)), true);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => document.querySelector('.sheet').contains(document.activeElement)), true);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.sheet'));
  await navigate(page, 'devices');
});

test('refresh preserves an unfinished group form and topology resources', async t => {
  const { page, requests } = await open(t);
  await navigate(page, 'receivers');
  await page.locator('.receiver-management > summary').click();
  await page.locator('[data-create-group] input[name="name"]').fill('正在编辑的组合');
  await page.locator('[data-create-group] input[name="speaker"]').first().check();
  await page.evaluate(data => window.__stateSocket.onmessage({ data: JSON.stringify({ type: 'status', data: { ...data, error_count: 1 } }) }), status);
  assert.equal(await page.locator('[data-create-group] input[name="name"]').inputValue(), '正在编辑的组合');
  assert.equal(await page.locator('[data-create-group] input[name="speaker"]').first().isChecked(), true);
  const topologyOpened = page.waitForRequest(r => new URL(r.url()).pathname.endsWith('/api/topology'));
  await navigate(page, 'topology');
  await topologyOpened;
  await page.waitForSelector('[data-topology-nodes] button', { state: 'attached' });
  const count = requests.filter(r => r.path === '/api/topology').length;
  await page.evaluate(() => window.dispatchEvent(new Event('micast:request-render')));
  assert.equal(requests.filter(r => r.path === '/api/topology').length, count);
});

test('high contrast overrides glass; text markup remains literal', async t => {
  const { page } = await open(t);
  await navigate(page, 'settings'); await page.emulateMedia({ contrast: 'more' });
  const style = await page.locator('.group').first().evaluate(e => ({ image: getComputedStyle(e).backgroundImage, blur: getComputedStyle(e).backdropFilter }));
  assert.equal(style.image, 'none'); assert.equal(style.blur, 'none');
  const literal = await page.evaluate(async () => {
    const { renderToast } = await import('/app/micast/src/components/toast.ts');
    const node = document.createElement('div'); node.innerHTML = renderToast({ visible: true, message: '<b data-probe>内容</b>' });
    return !node.querySelector('[data-probe]') && node.textContent.includes('<b');
  });
  assert.equal(literal, true);
});

test('EQ is editable by keyboard and commits numeric gains', async t => {
  const { page, requests } = await open(t, { viewport: { width: 1440, height: 900 } });
  await navigate(page, 'devices'); await page.locator('[data-device-header]').first().click();
  await page.locator('[data-tuning-open]').first().click(); await page.waitForSelector('[data-tuning-canvas]');
  const pointCanvas = page.locator('[data-tuning-canvas]');
  const pointBox = await pointCanvas.boundingBox();
  await pointCanvas.click({position:{x:44,y:24+(pointBox.height-54)/2}});
  assert.equal(await page.locator('[data-eq-field="gain"]').count(), 1);
  const input = page.locator('[data-eq-field="gain"]').first(); await input.fill('2.5'); await input.press('Tab');
  await page.waitForFunction(() => document.querySelector('[data-eq-field="gain"]').value === '2.5');
  await new Promise(r => setTimeout(r, 400));
  const save = requests.find(r => r.path === '/api/tuning/eq');
  assert.equal(JSON.parse(save.body).points[0][1], 2.5);

});

test('layout adapts at 320/390/768/1440 without narrow form columns', async t => {
  for (const width of [320,390,768,1440]) {
    const { page } = await open(t, { viewport: { width, height: 900 } });
    await navigate(page, 'receivers'); await page.locator('.receiver-management > summary').click();
    const geometry = await page.locator('.receiver-form').evaluate(e => ({ form: e.getBoundingClientRect().width, text: e.querySelector('.cell-content').getBoundingClientRect().width, select: e.querySelector('select').getBoundingClientRect().width, viewport: innerWidth, scroll: document.documentElement.scrollWidth }));
    assert.equal(geometry.scroll, geometry.viewport);
    if (width < 500) assert.ok(geometry.text > 220, `description receives a full row at ${width}px`);
    assert.ok(geometry.select > 120);
    const player = await page.locator('.now-playing').evaluate(e => ({ width: e.getBoundingClientRect().width, position: getComputedStyle(e).position, offsetParent: e.querySelector('.now-playing-controls').offsetParent === e }));
    if (width >= 1024) assert.ok(player.width > 100);
    else assert.equal(await page.locator('.header-playback-trigger.visible').isVisible(), true, 'mobile capsule remains reachable');
    if (width >= 1024) assert.equal(player.position, 'relative', 'glass is confined to its player surface');
    await page.close();
  }
});

test('mobile EQ requires explicit editing and locks again after leaving', async t => {
  const { page, requests } = await open(t, { isMobile: true, hasTouch: true });
  await navigate(page, 'devices'); await page.locator('[data-device-header]').first().click();
  await page.locator('[data-tuning-open]').first().click();
  const toggle = page.locator('[data-tuning-edit-toggle]');
  await toggle.waitFor();
  assert.equal(await toggle.getAttribute('aria-pressed'), 'false');

  assert.equal(await page.locator('[data-eq-point-editor]').isVisible(), false);
  await page.locator('[data-tuning-canvas]').tap({position:{x:100,y:100}});
  await page.waitForTimeout(400);
  assert.equal(requests.filter(r => r.path === '/api/tuning/eq').length, 0);
  const session = await page.context().newCDPSession(page);
  const canvas = await page.locator('[data-tuning-canvas]').boundingBox();
  const x = canvas.x + canvas.width / 2, y = canvas.y + canvas.height / 2;
  await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:1}]});
  await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-60,id:1}]});
  await page.waitForTimeout(60);
  await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x,y:y-130,id:1}]});
  await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
  await page.waitForTimeout(250);
  assert.ok(await page.locator('.app-body').evaluate(e => e.scrollTop > 0), 'locked curve allows touch scrolling');
  assert.equal(requests.filter(r => r.path === '/api/tuning/eq').length, 0);
  await toggle.click();
  const pointCanvas = page.locator('[data-tuning-canvas]');
  const pointBox = await pointCanvas.boundingBox();
  await pointCanvas.tap({position:{x:36,y:22+(pointBox.height-48)/2}});
  const input = page.locator('[data-eq-field="gain"]').first();
  assert.equal(await input.isEnabled(), true);
  await input.fill('2.5'); await input.press('Tab');
  await page.waitForTimeout(450);
  assert.equal(JSON.parse(requests.find(r => r.path === '/api/tuning/eq').body).points[0][1], 2.5);
  await toggle.click();
  assert.equal(await page.locator('[data-eq-point-editor]').isVisible(), false);
  await page.locator('[data-tuning-back]').click();
  await page.locator('[data-tuning-open]').first().click();
  assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
});

test('API errors preserve status independently of user-facing text', async t => {
  const { page } = await open(t);
  await page.route('**/api/topology', route => route.fulfill({status:404,contentType:'application/json',body:JSON.stringify({detail:'HTTP 404'})}));
  const error = await page.evaluate(async () => {
    const { api, ApiError } = await import('/app/micast/src/api.ts');
    try { await api.getTopology(); } catch (error) {
      return { typed: error instanceof ApiError, status: error.status, retryable: error.retryable, message: error.message };
    }
  });
  assert.equal(error.typed, true); assert.equal(error.status, 404); assert.equal(error.retryable, false);
  assert.equal(error.message.includes('HTTP 404'), false);
});

test('runtime discards old revisions and accepts a restarted backend', async t => {
  const { page } = await open(t);
  const result = await page.evaluate(async () => {
    const { store } = await import('/app/micast/src/state.ts');
    const base = store.get().status;
    const runtime = (epoch, revision) => ({ epoch, revision, sessions: [], targets: [] });
    store.set({status:{...base,runtime:runtime('first',10)}});
    store.set({status:{...base,runtime:runtime('first',9)}});
    const preserved = store.get().status.runtime.revision;
    store.set({status:{...base,runtime:runtime('second',1)}});
    store.set({status:{...base,runtime:runtime('first',11)}});
    return [preserved,store.get().status.runtime.epoch,store.get().status.runtime.revision];
  });
  assert.deepEqual(result,[10,'second',1]);
});

test('late volume confirmations cannot overwrite a newer request', async t => {
  const { page } = await open(t);
  const value = await page.evaluate(async () => {
    const { api } = await import('/app/micast/src/api.ts');
    const { setVolume } = await import('/app/micast/src/volume-service.ts');
    const { store } = await import('/app/micast/src/state.ts');
    const original = api.setVolume;
    const pending = [];
    api.setVolume = () => new Promise(resolve => pending.push(resolve));
    try {
      const first = setVolume(10,['d1']);
      const second = setVolume(20,['d1']);
      pending[1]({devices:[{did:'d1',ok:true,volume:20}]}); await second;
      pending[0]({devices:[{did:'d1',ok:true,volume:10}]}); await first;
      return store.get().devices.find(d => d.did === 'd1').volume;
    } finally { api.setVolume = original; }
  });
  assert.equal(value,20);
});

test('playback and topology reject reordered observations and retired epochs', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    const status = store.get().status, playback = store.get().playback;
    const runtime = (epoch,revision,sequence) => ({epoch,revision,sequence,sessions:[],targets:[]});
    store.set({status:{...status,runtime:runtime('ordering',10,10)}});
    store.set({playback:{...playback,volume:40,runtime:runtime('ordering',10,30)}});
    store.set({playback:{...playback,volume:20,runtime:runtime('ordering',10,20)}});
    const preserved = store.get().playback.volume;
    store.set({status:{...status,runtime:runtime('ordering',11,31)}});
    store.set({playback:{...playback,volume:25,runtime:runtime('ordering',10,32)}});
    const staleState = store.get().playback.volume;
    const freshTopology = store.acceptRuntime(runtime('ordering',11,40),'topology');
    const oldTopology = store.acceptRuntime(runtime('ordering',11,39),'topology');
    store.set({status:{...status,runtime:runtime('restarted',1,1)}});
    const reset = store.get().playback;
    store.set({playback:{...playback,volume:50,runtime:runtime('ordering',12,100)}});
    const legacy = store.acceptRuntime({revision:999,sessions:[],targets:[]},'status');
    return [preserved,staleState,freshTopology,oldTopology,reset,store.get().playback,legacy];
  });
  assert.deepEqual(result,[40,40,true,false,null,null,false]);
});

test('target takeover invalidates pending volume confirmations', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    const {api} = await import('/app/micast/src/api.ts');
    const {setVolume} = await import('/app/micast/src/volume-service.ts');
    const status = store.get().status;
    const runtime = (revision,owner,generation) => ({epoch:'ownership',revision,sequence:revision,
      sessions:[],targets:[{target:'speaker:d1',owner,generation}]});
    store.set({status:{...status,runtime:runtime(1,'old',1)}});
    let resolve;
    const original = api.setVolume;
    api.setVolume = () => new Promise(r=>{resolve=r;});
    try {
      const before = store.get().devices.find(d=>d.did==='d1').volume;
      const pending = setVolume(99,['d1']);
      store.set({status:{...status,runtime:runtime(2,'new',2)}});
      resolve({devices:[{did:'d1',ok:true,volume:99}]});
      await pending;
      return [before,store.get().devices.find(d=>d.did==='d1').volume];
    } finally {api.setVolume=original;}
  });
  assert.equal(result[0],result[1]);
});

test('account changes invalidate reads and configuration rejects old revisions', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    const first = store.beginRead('devices',true);
    const second = store.beginRead('devices',true);
    const oldRequest = first();
    store.set({xiaomi:{logged_in:false,user_id:null}});
    const oldAccount = second();
    const config = store.get().fullConfig;
    store.set({fullConfig:{...config,runtime_epoch:'config',config_revision:8,default_volume:80}});
    store.set({fullConfig:{...config,runtime_epoch:'config',config_revision:7,default_volume:70}});
    return [oldRequest,oldAccount,store.get().fullConfig.default_volume];
  });
  assert.deepEqual(result,[false,false,80]);
});

test('player metadata follows target ownership and disables unavailable volume controls', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    const {primaryTrack,renderPlaybackBar} = await import('/app/micast/src/components/playback-bar.ts');
    const base = store.get().status, playback = store.get().playback;
    const runtime = {epoch:'metadata',revision:1,sequence:1,
      sessions:[{owner:'dlna:r',state:'active'}],
      targets:[{target:'speaker:d1',owner:'dlna:r',generation:2,capabilities:{volume_control:false}}]};
    const track = title=>({title,artist:null,album:null,lyric_lines:null,audio_id:null,duration:null,cover:null});
    const status = {...base,runtime,now_playing:{old:track('Old'), 'dlna:r':track('Current')}};
    store.set({status,playback:{...playback,runtime,devices:[playback.devices[0]]}});
    const node=document.createElement('div');node.innerHTML=renderPlaybackBar(store.get().playback);
    return [primaryTrack(store.get().status).title,node.querySelector('[data-master-slider]').disabled,
      node.querySelector('[data-mute]').disabled];
  });
  assert.deepEqual(result,['Current',true,true]);
});

test('closing calibration while microphone permission is pending stops the late stream', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {CalibrationWizard} = await import('/app/micast/src/components/calibration-wizard.ts');
    let resolve,stops=0;
    const original = navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia=()=>new Promise(r=>{resolve=r;});
    try {
      const wizard=new CalibrationWizard(document.createElement('div'),'d1',()=>{});
      const pending=wizard.measure('d1',true);
      wizard.destroy();
      resolve({getTracks:()=>[{stop:()=>{stops++;}}]});
      const value=await pending;
      return [stops,value];
    } finally {navigator.mediaDevices.getUserMedia=original;}
  });
  assert.deepEqual(result,[1,null]);
});

test('cancelled live calibration waits for pending writes before restoring delay', async t => {
  const {page} = await open(t);
  const result = await page.evaluate(async () => {
    const {store} = await import('/app/micast/src/state.ts');
    const {api} = await import('/app/micast/src/api.ts');
    const {liveCalibration} = await import('/app/micast/src/components/calibration.ts');
    const group={...store.get().fullConfig.groups[0],speaker_ids:['d1','d2'],anchor_did:'d1',delays_ms:{d2:20}};
    const calls=[];
    let release;
    const original=api.updateGroup;
    api.updateGroup=(id,patch)=>{
      calls.push({...patch.delays_ms});
      if(calls.length===1) return new Promise(resolve=>{release=()=>resolve({...group,...patch});});
      return Promise.resolve({...group,...patch});
    };
    try {
      const completion=liveCalibration(group,store.get());
      document.querySelector('[data-nudge="10"]').click();
      await new Promise(resolve=>setTimeout(resolve,0));
      document.querySelector('[data-live-cancel]').click();
      await new Promise(resolve=>setTimeout(resolve,0));
      const before=calls.length;
      release();
      const saved=await completion;
      return [before,saved,calls,Boolean(document.querySelector('.calibration-dialog'))];
    } finally {api.updateGroup=original;}
  });
  assert.deepEqual(result,[1,false,[{d2:30},{d2:20}],false]);
});

test('business connection bounds a hanging handshake and disposes resources', async t => {
  const { page } = await open(t);
  await page.clock.install();
  await page.evaluate(async () => {
    window.WebSocket = class { static OPEN=1; static CONNECTING=0; readyState=0; close() { this.readyState=3; } };
    const { RealtimeConnection } = await import('/app/micast/src/realtime.ts');
    window.__probeCount = 0;
    window.__probeConnection = new RealtimeConnection({message:()=>{},refresh:async()=>{},fallback:async()=>{window.__probeCount++;}});
    window.__probeConnection.start();
  });
  await page.clock.runFor(8500);
  assert.equal(await page.evaluate(() => window.__probeConnection.state), 'fallback');
  assert.ok(await page.evaluate(() => window.__probeCount > 0));
  await page.evaluate(() => window.__probeConnection.dispose());
  const count = await page.evaluate(() => window.__probeCount);
  await page.clock.runFor(35000);
  assert.equal(await page.evaluate(() => window.__probeCount), count);
});

test('spectrum handshake times out, polls without overlap, and stops after disposal', async t => {
  const { page } = await open(t);
  await page.clock.install();
  await page.evaluate(async () => {
    window.WebSocket = class { static OPEN=1; readyState=0; close() { this.readyState=3; } };
    const { SpectrumFeed } = await import('/app/micast/src/components/spectrum-feed.ts');
    const { api } = await import('/app/micast/src/api.ts');
    window.__spectrumCalls=0; window.__spectrumUpdates=[];
    api.getSpectrum=async()=>{window.__spectrumCalls++; return {bands:[0.25]};};
    window.__spectrumFeed=new SpectrumFeed(bands=>window.__spectrumUpdates.push(bands));
    window.__spectrumFeed.attach('d1');
  });
  await page.clock.runFor(43000);
  assert.ok(await page.evaluate(()=>window.__spectrumCalls>0));
  assert.ok(await page.evaluate(()=>window.__spectrumUpdates.some(value=>value?.[0]===0.25)));
  await page.evaluate(()=>window.__spectrumFeed.destroy());
  const before=await page.evaluate(()=>[window.__spectrumCalls,window.__spectrumUpdates.length]);
  await page.clock.runFor(3000);
  assert.deepEqual(await page.evaluate(()=>[window.__spectrumCalls,window.__spectrumUpdates.length]),before);
});


test('navigation stays under the pointer until release, without press jumps', async t => {
  for (const width of [390,1440]) {
    const {page} = await open(t,{viewport:{width,height:900}});
    await navigate(page,'devices');
    await page.waitForTimeout(650);
    const lens = page.locator(width < 1024 ? '.tab-lens' : '.nav-lens');
    const r = await lens.boundingBox();
    const x = r.x + r.width/2, y = r.y + r.height/2;
    await page.mouse.move(x,y); await page.waitForTimeout(250);
    const before = await lens.boundingBox();
    await page.mouse.down(); await page.waitForTimeout(200);
    const pressed = await lens.boundingBox();
    assert.ok(Math.abs(before.x + before.width/2 - pressed.x - pressed.width/2) < 1);
    assert.ok(Math.abs(before.y + before.height/2 - pressed.y - pressed.height/2) < 1);
    const delta = width < 1024 ? 65 : 60;
    await page.mouse.move(x + (width < 1024 ? delta:0),y + (width < 1024 ? 0:delta));
    await page.waitForTimeout(750);
    assert.ok(page.url().endsWith('#devices'), 'no selection while holding');
    const dragged = await lens.boundingBox();
    const displacement = width < 1024 ? dragged.x-pressed.x : dragged.y-pressed.y;
    assert.ok(Math.abs(displacement-delta)<2, `1:1 movement: ${displacement}`);
    await page.mouse.up();
    await page.waitForFunction(() => location.hash === '#topology');
    await page.close();
  }
});

test('touch capture transfer never settles a live drag', async t => {
  const {page} = await open(t,{hasTouch:true,isMobile:true});
  await navigate(page,'devices'); await page.waitForTimeout(650);
  const session = await page.context().newCDPSession(page);
  const r = await page.locator('.tab-lens').boundingBox();
  const x = r.x+r.width/2, y = r.y+r.height/2;
  const touch = (type,dx=0) => session.send('Input.dispatchTouchEvent',{type,touchPoints:type==='touchEnd'?[]:[{x:x+dx,y,id:1}]});
  await touch('touchStart'); await touch('touchMove',65); await page.waitForTimeout(800);
  assert.ok(page.url().endsWith('#devices'));
  const moved = await page.locator('.tab-lens').boundingBox();
  assert.ok(Math.abs(moved.x-r.x-65)<3);
  await touch('touchEnd'); await page.waitForFunction(()=>location.hash==='#topology');
});

for (const [device, viewport] of [['desktop', {width:1280,height:900}], ['mobile', {width:390,height:844}]]) {
  test(`protocol recovery preserves disabled features on ${device}`, async t => {
    let current = {...config, airplay_enabled:true, airplay2_enabled:true, airplay2_available:true, dlna_enabled:false, ports:[{id:'airplay2_port',name:'AirPlay 2',protocol:'tcp',mode:'fixed',preferred:7000,actual:null,status:'error',editable:false,detail:'固定 TCP 7000 被占用，释放后重新启动'}], protocol_status:{
      airplay:{status:'ready',detail:'可连接'},
      airplay2:{status:'blocked',detail:'时钟端口被占用，请释放后重新检测'},
      dlna:{status:'disabled',detail:'用户已关闭'},
    }};
    const {page,requests} = await open(t,{viewport},{
      '/api/config':async()=>current,
      '/api/config/protocols/retry':async()=>{
        current={...current,protocol_status:{...current.protocol_status,airplay2:{status:'ready',detail:'可连接'}}};
        return {protocol_status:current.protocol_status};
      },
      '/api/config/airplay':async route=>{
        const enabled=JSON.parse(route.request().postData()).enabled;
        current={...current,airplay_enabled:enabled,protocol_status:{...current.protocol_status,airplay:{status:'disabled',detail:'用户已关闭'}}};
        return {airplay_enabled:enabled};
      },
    });
    await navigate(page,'settings');
    assert.equal(await page.locator('[data-port-input="airplay2_port"]').count(),0);
    await page.getByText('固定 TCP 7000 被占用，释放后重新启动',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-retry-protocol="dlna"]').count(),0);
    assert.equal(await page.locator('#airplay2-enabled').count(),1);
    assert.equal(await page.locator('#dlna-enabled').count(),1);
    assert.equal(await page.evaluate(()=>document.querySelector('#airplay2-enabled').closest('.group').previousElementSibling.textContent.trim()),'实验功能');
    const panel=page.locator('.group').filter({has:page.locator('#airplay-enabled')});

    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.locator('[data-retry-protocol="airplay2"]').click();
    await page.locator('.cell').filter({has:page.locator('#airplay2-enabled')}).getByText('可用 · 可连接',{exact:true}).waitFor();
    assert.equal(await page.locator('[data-retry-protocol="airplay2"]').count(),0);
    assert.equal(current.dlna_enabled,false);
    assert.deepEqual(JSON.parse(requests.find(r=>r.path==='/api/config/protocols/retry').body),{protocol:'airplay2'});
    await page.locator('#airplay-enabled').uncheck();
    await page.locator('.cell').filter({has:page.locator('#airplay-enabled')}).getByText(/已关闭 ·/).waitFor();
    assert.deepEqual(JSON.parse(requests.find(r=>r.path==='/api/config/airplay').body),{enabled:false});
  });
}


test('fullscreen retains its lyric surface when sender lines disappear', async t => {
  const {page}=await open(t,{viewport:{width:1440,height:900}});
  await page.evaluate(async()=>{(await import('/app/micast/src/player/controller.ts')).openPlayerFullscreen();});
  await page.waitForSelector('[data-player-lyrics]');
  await page.evaluate(()=>window.__lyricSurface=document.querySelector('[data-player-lyrics]'));
  for(const lines of [[],['new line'],['new line','next line']]) {
    await page.evaluate(async lines=>{const {store}=await import('/app/micast/src/state.ts');const current=store.get();store.set({status:{...current.status,now_playing:{r1:{...current.status.now_playing.r1,lyric_lines:lines}}}});window.dispatchEvent(new Event('micast:request-render'));},lines);
    assert.equal(await page.evaluate(()=>window.__lyricSurface===document.querySelector('[data-player-lyrics]')),true);
  }
});

test('new point opens the persistent editor and blur cannot swallow delete', async t => {
  const {page,requests}=await open(t,{viewport:{width:1440,height:900}});
  await navigate(page,'devices');await page.locator('[data-device-header]').first().click();await page.locator('[data-tuning-open]').first().click();
  await page.locator('[data-tuning-canvas]').click({position:{x:310,y:70}});
  assert.equal(await page.locator('[data-eq-point-editor]').isVisible(),true);
  const remove=page.locator('[data-eq-remove]');await remove.evaluate(node=>window.__removeNode=node);
  const gain=page.locator('[data-eq-field="gain"]');await gain.fill('3.1');await remove.click();
  assert.equal(await page.evaluate(()=>window.__removeNode.isConnected),true);
  assert.equal(await page.locator('[data-eq-point-editor]').isVisible(),false);
  await page.waitForTimeout(450);
  const writes=requests.filter(r=>r.path==='/api/tuning/eq'&&r.body);
  assert.equal(JSON.parse(writes.at(-1).body).points.length,5);
});

test('diagnostic refresh retains the log node and does not schedule a scroll rewind', async t => {
  const {page}=await open(t,{}, {'/api/debug/state':async()=>({
    logged_in:true,selected_device_id:'d1',devices:[],pcm_source:'AirPlay',stream_url:'',audio_config:config.audio,
    bridge_status:{status:'running',error_count:0},stream_clients:0,stream_bytes_sent:0,diagnostics:{raop:{},streams:{}},
    logs:{items:[],total:0,shown:0,truncated:false,covered:{from:null,to:null},buffer_total:0,buffer_capacity:100,server_time:Date.now()/1000,new_count:0,buckets:{}}
  })});await navigate(page,'debug');await page.waitForSelector('[data-runtime-log]',{state:'attached'});
  const result=await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');const body=document.querySelector('.app-body');const log=document.querySelector('[data-runtime-log]');
    document.querySelector('[data-debug-section="log"]').open=true;
    body.scrollTop=100;window.dispatchEvent(new Event('micast:request-render'));body.scrollTop=150;
    const intended=body.scrollTop;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    return {same:log===document.querySelector('[data-runtime-log]'),expected:intended,actual:body.scrollTop};
  });
  assert.equal(result.same,true);assert.equal(result.actual,result.expected);
  await page.locator('[data-debug-section="test"] > summary').click();
  await page.locator('label').filter({has:page.locator('input[name="debug-source"][value="upload"]')}).click();
  await page.waitForSelector('[data-test-file]',{state:'attached'});

});


test('last EQ point can be deleted from numeric editor', async t => {
  const {page,requests}=await open(t,{viewport:{width:1440,height:900}}, {
    '/api/devices':async()=>devices.map(d=>({...d,eq:{...d.eq,points:[[1000,0]]}})),
    '/api/tuning/d1':async()=>({did:'d1',enabled:true,points:[[1000,0]],preset:'',target:'',revision:1})
  });
  await navigate(page,'devices');
  await page.locator('[data-device-header]').first().click();
  await page.locator('[data-tuning-open]').first().click();
  const canvas=page.locator('[data-tuning-canvas]');
  const box=await canvas.boundingBox();
  await canvas.click({position:{x:44+(box.width-60)*Math.log(50)/Math.log(1000),y:24+(box.height-54)/2}});
  const remove=page.locator('[data-eq-remove]');
  await remove.waitFor();assert.equal(await remove.isEnabled(),true);
  await remove.click();
  assert.equal(await page.locator('[data-eq-point-editor]').isVisible(),false);
  await page.waitForTimeout(450);
  const writes=requests.filter(r=>r.path==='/api/tuning/eq'&&r.body);
  assert.equal(JSON.parse(writes.at(-1).body).points.length,0);
});

test('degraded topology is explained in Chinese', async t => {
  const {page}=await open(t);
  await navigate(page,'topology');
  await page.evaluate(async()=>{
    const {topology}=await import('/app/micast/tests/fixtures.mjs');
    window.dispatchEvent(new CustomEvent('micast:topology',{detail:{...topology,status:'degraded'}}));
  });
  await page.locator('[data-topology-status]').getByText('部分功能不可用',{exact:true}).waitFor();
});


test('partial receiver failure is not labelled service stopped', async t => {
  const {page}=await open(t);
  const markup=await page.evaluate(async()=>{
    const {renderStatusOverview}=await import('/app/micast/src/components/debug-panel.ts');
    const {store}=await import('/app/micast/src/state.ts');
    return renderStatusOverview({bridge_status:{status:'degraded',error_count:0},diagnostics:{raop:{},streams:{}},audio_config:{format:'mp3',sample_rate:48000}},store.get());
  });
  assert.equal(markup.includes('服务未运行'),false);
  assert.equal(markup.includes('部分功能不可用'),true);
});


for (const [device, viewport] of [['desktop',{width:1440,height:900}],['mobile',{width:390,height:844}]]) {
  test(`fullscreen content layout follows metadata on ${device}`, async t => {
    const {page}=await open(t,{viewport}, {'/api/playback/state':async()=>({...playback,devices:[playback.devices[0]]})});
    await page.evaluate(async()=>{
      const {store}=await import('/app/micast/src/state.ts');
      store.set({status:{...store.get().status,now_playing:{}}});
      (await import('/app/micast/src/player/controller.ts')).openPlayerFullscreen();
    });
    const body=page.locator('[data-player-layout]');
    await body.waitFor();
    for (const [protocol,label] of [['airplay','AirPlay'],['airplay2','AirPlay 2'],['dlna','DLNA']]) {
      await page.evaluate(async protocol=>{
        const {store}=await import('/app/micast/src/state.ts');
        const state=store.get();
        const runtime={epoch:store.runtimeGeneration,revision:100,sequence:100,
          sessions:[{owner:'r1',protocol,state:'active',generation:1,resources:[],capabilities:{}}],
          targets:state.playback.devices.map(d=>({target:`speaker:${d.did}`,owner:'r1',generation:1}))};
        store.set({status:{...state.status,runtime,now_playing:{}},playback:{...state.playback,runtime}});
        window.dispatchEvent(new Event('micast:request-render'));
      },protocol);
      await page.getByText(`正在通过 ${label} 播放`,{exact:true}).waitFor();
      assert.equal(await body.getAttribute('data-player-layout'),'centered');
      assert.equal(await page.locator('[data-player-lyrics]').isVisible(),false);
      assert.equal(await page.locator('[data-player-content-hint]').isVisible(),true);
    }
    await page.waitForTimeout(300);
    const center=await page.locator('.player-overlay-identity').boundingBox();
    const area=await body.boundingBox();
    assert.ok(Math.abs(center.x+center.width/2-area.x-area.width/2)<2);

    await page.evaluate(()=>window.__persistentLyrics=document.querySelector('[data-player-lyrics]'));
    for (const lines of [[],['第一句歌词'],[],['第二句歌词']]) {
      await page.evaluate(async lines=>{
        const {store}=await import('/app/micast/src/state.ts');
        store.set({status:{...store.get().status,now_playing:{r1:{title:'测试歌曲',artist:'测试歌手',lyric_lines:lines}}}});
        window.dispatchEvent(new Event('micast:request-render'));
      },lines);
      await page.waitForFunction(expected=>document.querySelector('[data-player-layout]').dataset.playerLayout===expected,lines.length?'lyrics':'centered');
      assert.equal(await page.locator('[data-player-content-hint]').isVisible(),false);
      assert.equal(await page.evaluate(()=>window.__persistentLyrics===document.querySelector('[data-player-lyrics]')),true);
    }
    await page.waitForTimeout(300);

    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  });
}


for (const viewport of [{width:1440,height:900},{width:390,height:844}]) {
  test(`diagnostic deep scrolling survives growing logs and status ticks ${viewport.width}`, async t => {
    const debug={logged_in:true,selected_device_id:'d1',devices:[],pcm_source:'AirPlay',stream_url:'',audio_config:config.audio,
      bridge_status:{status:'running',error_count:0},stream_clients:0,stream_bytes_sent:0,diagnostics:{raop:{},streams:{}},
      logs:{items:Array.from({length:80},(_,i)=>({time:'19:47:00',level:'INFO',logger:'test',message:`播放记录 ${i}`,ts:i})),total:80,shown:80,truncated:false,covered:{from:null,to:null},buffer_total:80,buffer_capacity:100,server_time:Date.now()/1000,new_count:0,buckets:{}}};
    const {page}=await open(t,{viewport},{'/api/debug/state':async()=>debug});
    await navigate(page,'debug');
    await page.locator('[data-debug-section="log"] > summary').click();
    const result=await page.evaluate(async debug=>{
      const {store}=await import('/app/micast/src/state.ts');
      const {updateDebugPanel}=await import('/app/micast/src/components/debug-panel.ts');
      const body=document.querySelector('.app-body'), main=document.querySelector('.main-content');
      const log=document.querySelector('[data-runtime-log]');
      body.scrollTop=Math.min(700,body.scrollHeight-body.clientHeight);
      log.scrollTop=100;
      const top=body.scrollTop, inner=log.scrollTop;
      for(let i=0;i<10;i++) {
        debug={...debug,logs:{...debug.logs,items:[...debug.logs.items,{time:'19:48:00',level:'INFO',logger:'test',message:`新增 ${i}`,ts:100+i}]}};
        store.set({debug}); updateDebugPanel(main,store.get());
        window.dispatchEvent(new Event('micast:request-render'));
        await new Promise(r=>requestAnimationFrame(r));
      }
      return {top,actual:body.scrollTop,inner,innerActual:log.scrollTop,same:log===document.querySelector('[data-runtime-log]'),open:document.querySelector('[data-debug-section="log"]').open};
    },debug);
    assert.ok(result.top>0,'exercise an actually scrolled page');
    assert.equal(result.actual,result.top);assert.equal(result.innerActual,result.inner);
    assert.equal(result.same,true);assert.equal(result.open,true);

  });
}


test('dark navigation uses readable text accent and port fields fit five digits', async t => {
  const {page}=await open(t,{viewport:{width:1440,height:900},colorScheme:'dark'}, {
    '/api/config':async()=>({...config,ports:[{id:'stream_port',name:'音频流服务',mode:'auto',editable:true,preferred:42400,actual:42400,protocol:'tcp',status:'listening',detail:'音箱拉取音频流'}]})
  });
  await navigate(page,'settings');
  const input=page.locator('.port-control .settings-number');
  await input.waitFor();
  await page.waitForTimeout(600);
  const metrics=await page.evaluate(()=>{
    const input=document.querySelector('.port-control .settings-number');
    const color=getComputedStyle(document.querySelector('.sidebar .nav-item.active')).color;
    return {color,width:input.getBoundingClientRect().width,height:input.getBoundingClientRect().height,font:parseFloat(getComputedStyle(input).fontSize),scrollWidth:input.scrollWidth,clientWidth:input.clientWidth};
  });
  assert.equal(metrics.color,'rgb(214, 235, 255)');
  assert.ok(metrics.width<=95);assert.ok(metrics.height<=42);assert.ok(metrics.font<=16);
  assert.ok(metrics.scrollWidth<=metrics.clientWidth);
  await input.scrollIntoViewIfNeeded();

});


test('page navigation sends no playback mutations and settings updates retain deep scroll', async t => {
  const {page,requests}=await open(t,{viewport:{width:1440,height:900}});
  for (const section of ['devices','topology','settings','receivers','settings']) await navigate(page,section);
  assert.deepEqual(requests.filter(r=>r.method!=='GET'),[]);
  const position=await page.evaluate(async()=>{
    const {store}=await import('/app/micast/src/state.ts');
    const body=document.querySelector('.app-body');body.scrollTop=500;
    const top=body.scrollTop;
    for(let i=0;i<8;i++) {
      store.set({status:{...store.get().status,status:i%2?'running':'degraded'}});
      window.dispatchEvent(new Event('micast:request-render'));
      await new Promise(r=>requestAnimationFrame(r));
    }
    return {top,actual:body.scrollTop,playing:store.get().playback.playing};
  });
  assert.ok(position.top>0);assert.equal(position.actual,position.top);assert.equal(position.playing,true);
});


test('failed connection retry remains recoverable without an uncaught rejection', async t => {
  const {page} = await open(t);
  await page.route('**/retry-audit', route => route.fulfill({contentType:'text/html',body:'<div data-connection-notice hidden><button data-connection-retry>Retry</button></div>'}));
  await page.goto(new URL('retry-audit', url).href);
  await page.evaluate(async () => {
    const {bindConnectivity} = await import('/app/micast/src/connectivity.ts');
    window.__retryUnhandled = 0;
    window.addEventListener('unhandledrejection', () => window.__retryUnhandled++);
    bindConnectivity(async () => { throw new Error('service unavailable'); });
    document.querySelector('[data-connection-notice]').hidden = false;
  });
  await page.locator('[data-connection-retry]').click();
  await page.waitForTimeout(100);
  assert.equal(await page.locator('[data-connection-retry]').isEnabled(),true);
  assert.equal(await page.locator('[data-connection-notice]').isVisible(),true);
  assert.equal(await page.evaluate(()=>window.__retryUnhandled),0);
});

for (const [width, reducedMotion] of [[1440, 'no-preference'], [390, 'no-preference'], [390, 'reduce']]) {
  test(`topology keeps a gentle breeze after settling ${width} ${reducedMotion}`, async t => {
    const {page} = await open(t, {viewport:{width,height:900}, reducedMotion});
    await page.clock.install();
    await navigate(page, 'topology');
    await page.evaluate(() => {
      window.__topologyPaint = {};
      const arc = CanvasRenderingContext2D.prototype.arc;
      CanvasRenderingContext2D.prototype.arc = function(x,y,r,...rest) {
        if (this.canvas.matches('[data-topology-canvas]') && r > 4 && r < 6) {
          window.__topologyPaint[`${this.fillStyle}:${r.toFixed(2)}`] = {x,y};
        }
        return arc.call(this,x,y,r,...rest);
      };
    });
    await page.clock.runFor(25000);
    const read = () => page.evaluate(() => structuredClone(window.__topologyPaint));
    let previous = await read();
    assert.equal(Object.keys(previous).length,3);
    for (let interval=0; interval<3; interval++) {
      await page.clock.runFor(5000);
      const next = await read();
      const distances = Object.keys(next).map(key => Math.hypot(next[key].x-previous[key].x,next[key].y-previous[key].y));
      if (reducedMotion === 'reduce') assert.ok(Math.max(...distances)<.2,'reduced motion keeps settled nodes still');
      else assert.ok(Math.max(...distances)>1,'nodes continue moving after the initial layout settles');
      previous = next;
    }
    const beforeUpdate = await read();
    await page.evaluate(async () => {
      const {topology} = await import('/app/micast/tests/fixtures.mjs');
      window.dispatchEvent(new CustomEvent('micast:topology',{detail:{...topology,ts:Date.now()/1000}}));
    });
    await page.clock.runFor(50);
    const afterUpdate = await read();
    assert.ok(Math.hypot(afterUpdate['#34d399:4.34'].x-beforeUpdate['#34d399:4.34'].x,afterUpdate['#34d399:4.34'].y-beforeUpdate['#34d399:4.34'].y)<1,'refresh does not restart the breeze');
    const point = afterUpdate['#34d399:4.34'];
    await page.locator('[data-topology-canvas]').click({position:point});
    await page.locator('.topology-detail-head').getByText('客厅',{exact:true}).waitFor();
  });
}

for (const viewport of [{width:1440,height:900},{width:390,height:844},{width:390,height:568},{width:844,height:390}]) {
 test(`setup fits every step ${viewport.width}x${viewport.height}`,async t=>{
  const {page}=await open(t,{viewport});
  for(const step of ['access','xiaomi','receivers','airplay2','complete']){
   await page.evaluate(async step=>{const {store}=await import('/app/micast/src/state.ts');store.set({access:{...store.get().access,setup_complete:false,access_configured:step!=='access'},onboardingStep:step,fullConfig:{...store.get().fullConfig,airplay2_available:true},xiaomi:{logged_in:false}});window.dispatchEvent(new Event('micast:request-render'));},step);
   if(step==='airplay2') await page.locator('input[name="airplay2_enabled"][value="true"]').check();
   const d=await page.locator('.setup-page').evaluate(e=>({height:e.clientHeight,scroll:e.scrollHeight,bottom:e.querySelector('.setup-content').getBoundingClientRect().bottom}));
   assert.ok(d.scroll<=d.height+1&&d.bottom<=viewport.height+1,`${step} overflow ${JSON.stringify(d)}`);
  }
 });
}
test('fresh setup ignores stale dark preference',async t=>{
 const context=await browser.newContext({colorScheme:'dark'});t.after(()=>context.close());const page=await context.newPage();
 await page.addInitScript(()=>localStorage.setItem('micast-ui',JSON.stringify({theme:'dark'})));
 await installFixtures(page,{'/api/access/status':async()=>({access_configured:false,setup_complete:false,auth_enabled:false,authenticated:true})});
 await page.goto(url);await page.waitForSelector('[data-setup-access]');assert.equal(await page.locator('html').getAttribute('data-theme'),'light');
});
test('mobile mini centers cover and fallback and separates drag from click',async t=>{
 const {page}=await open(t);
 for(const cover of [null,{url:'assets/brands/mijia-app.png',rev:'test'}]){
 await page.evaluate(async cover=>{const {store}=await import('/app/micast/src/state.ts');store.set({status:{...store.get().status,now_playing:{r1:{title:'测试',cover}}}});window.dispatchEvent(new Event('micast:request-render'));},cover);
 const g=await page.locator('.header-playback-trigger.visible').evaluate(e=>{const r=e.getBoundingClientRect(),c=(e.querySelector('img')??e.querySelector('svg')).getBoundingClientRect();return {w:r.width,h:r.height,dx:c.x+c.width/2-r.x-r.width/2,dy:c.y+c.height/2-r.y-r.height/2};});assert.equal(g.w,56);assert.equal(g.h,56);assert.ok(Math.abs(g.dx)<1&&Math.abs(g.dy)<1,JSON.stringify(g));
 }
 const mini=page.locator('.header-playback-trigger.visible'),r=await mini.boundingBox();await page.mouse.move(r.x+28,r.y+28);await page.mouse.down();await page.mouse.move(45,r.y-40,{steps:8});await page.mouse.up();await page.waitForTimeout(250);assert.equal(await mini.count(),1);assert.equal(Math.round((await mini.boundingBox()).x),12);await mini.click();assert.equal(await page.locator('.now-playing:not(.is-minimized)').count(),1);
});

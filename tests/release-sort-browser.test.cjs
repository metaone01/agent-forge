/* Uses the existing Edge installation and Node CDP; all remote previews are intercepted. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_RELEASE_SORT_UI_SITE&&process.env.FORGE_RELEASE_SORT_FIXTURE&&process.env.FORGE_RELEASE_SORT_UI_ARTIFACTS;

test('Release order UI: full SQLite pages, explicit unknowns, filtering and old capabilities',{skip:!enabled,timeout:120000},async()=>{
  const site=path.resolve(process.env.FORGE_RELEASE_SORT_UI_SITE),artifacts=path.resolve(process.env.FORGE_RELEASE_SORT_UI_ARTIFACTS);
  fs.mkdirSync(artifacts,{recursive:true});
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_RELEASE_SORT_FIXTURE,'utf8'));
  const key=(revision,route,params)=>JSON.stringify([revision,route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(item=>[key(item.revision,item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],assetErrors=[],checks=[];
  let current='release-one',failOnce=false,retired=false,slowOnce=false,oldMode=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length)),params=Object.fromEntries(url.searchParams);
      requests.push({route,params,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      const revision=params.revision||current;
      let value=cases.get(key(revision,route,params));
      if(route==='/api/v1/packages'&&failOnce){failOnce=false;value={status:503,body:{error:{code:'service_busy'}}};}
      if(route==='/api/v1/packages'&&params.cursor&&retired)value={status:410,body:{error:{code:'cursor_expired'}}};
      if(oldMode&&route==='/api/v1/manifest'&&value){
        value=structuredClone(value);delete value.body.features.releaseSort;delete value.body.releaseSortCoverage;delete value.body.releaseSortPolicyVersion;value.body.availableSorts=value.body.availableSorts.filter(s=>s!=='latest_release');
      }
      if(oldMode&&route==='/api/v1/packages'&&value&&params.sort!=='latest_release'){value=structuredClone(value);delete value.body.releaseSortCoverage;for(const item of value.body.items)delete item.latestRelease;}
      if(oldMode&&route==='/api/v1/packages'&&params.sort==='latest_release')value={status:400,body:{error:{code:'sort_unavailable'}}};
      if(!value){unmatched.push({route,params,revision});res.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({error:{code:'fixture_missing'}}));return;}
      const reply=()=>{if(!res.destroyed)res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));};
      if(route==='/api/v1/packages'&&slowOnce){slowOnce=false;setTimeout(reply,1000);}else reply();return;
    }
    const route=decodeURIComponent(url.pathname).replace(/^\/project\//,'');
    const file=path.resolve(site,route.endsWith('/')?route+'index.html':route);
    if(!file.startsWith(site+path.sep)){res.writeHead(403).end();return;}
    try{
      const content=fs.readFileSync(file),type={'.js':'application/javascript','.css':'text/css','.html':'text/html','.json':'application/json','.png':'image/png','.md':'text/plain'}[path.extname(file)]||'application/octet-stream';
      res.writeHead(200,{'Content-Type':type}).end(content);
    }catch(_){if(route.startsWith('assets/'))assetErrors.push(url.href);res.writeHead(404).end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port+'/project/';
  const profile=fs.mkdtempSync(path.join(artifacts,'profile-'));
  const browser=spawn(process.env.FORGE_UI_BROWSER,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket,send,session,failure;
  try{
    let debug;
    for(let attempt=0;attempt<100;attempt++){try{debug=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split(/\r?\n/);break;}catch(_){await timers.setTimeout(100);}}
    assert.ok(debug,'isolated Edge exposes its own debugging port');
    socket=new WebSocket('ws://127.0.0.1:'+debug[0]+debug[1]);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    let sequence=0;const pending=new Map();
    socket.addEventListener('message',event=>{
      const value=JSON.parse(event.data);
      if(value.method==='Runtime.exceptionThrown')exceptions.push(value.params.exceptionDetails);
      if(value.method==='Network.requestWillBeSent')network.push(value.params.request.url);
      if(value.method==='Fetch.requestPaused'){
        const request=value.params,failed=request.request.url.endsWith('/failed.png');
        send('Fetch.fulfillRequest',{requestId:request.requestId,responseCode:failed?404:200,responseHeaders:[{name:'Content-Type',value:'image/png'}],body:failed?'':'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6V8AAAAASUVORK5CYII='},value.sessionId).catch(error=>exceptions.push({message:error.message}));
      }
      if(value.id&&pending.has(value.id)){const item=pending.get(value.id);pending.delete(value.id);clearTimeout(item.timer);value.error?item.reject(new Error(JSON.stringify(value.error))):item.resolve(value.result);}
    });
    send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++sequence,timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timed out '+method));},10000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
    const target=await send('Target.createTarget',{url:'about:blank'});session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
    await send('Page.enable',{},session);await send('Runtime.enable',{},session);await send('Network.enable',{},session);
    await send('Network.setBlockedURLs',{urls:['*://me.kis.v2.scr.kaspersky-labs.com/*']},session);
    await send('Fetch.enable',{patterns:[{urlPattern:'https://media.example.test/*'},{urlPattern:'https://github.com/metaone01/agent-forge/actions/workflows/*/badge.svg'}]},session);
    const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const until=async expression=>{const start=Date.now();while(Date.now()-start<8000){if(await evaluate(expression))return;await timers.setTimeout(50);}throw new Error('Timed out: '+expression);};
    const screenshot=async name=>{const value=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);fs.writeFileSync(path.join(artifacts,name),Buffer.from(value.data,'base64'));};
    const width=async value=>{await send('Emulation.setDeviceMetricsOverride',{width:value,height:1000,deviceScaleFactor:1,mobile:value<600},session);};
    const fits=async()=>assert.ok(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'page fits viewport');
    const background=async()=>evaluate('getComputedStyle(document.body).backgroundImage');
    const ready="document.querySelectorAll('.result-card').length>0 && document.querySelector('#results')?.getAttribute('aria-busy')==='false' && document.querySelector('#query-error').hidden";
    const ids=()=>evaluate("Array.from(document.querySelectorAll('.result-link')).map(a=>new URL(a.href).hash)");
    const sortUrl=base+'catalog/?revision=release-one&sort=latest_release';
    await width(1365);await send('Page.navigate',{url:sortUrl},session);await until(ready);
    assert.equal(await evaluate("document.querySelector('#sort-select').value"),'latest_release');
    assert.equal(await evaluate("document.querySelectorAll('.result-card').length"),20);
    assert.ok((await evaluate("document.querySelector('#ranking-context').textContent")).includes('未来时间排除'));
    assert.ok((await evaluate("document.querySelector('.release-sort-fact').textContent")).includes('规范版本 2.0.0'));
    const first=await ids();current='release-two';
    await evaluate("document.querySelector('#next-page').click()");await until(ready+" && document.querySelectorAll('.result-card').length===8");
    const second=await ids();assert.equal(new Set([...first,...second]).size,28);
    assert.ok(second.at(-1).includes('plugin.gamma'));assert.ok((await evaluate("document.querySelectorAll('.release-sort-fact')[7].textContent")).includes('未知'));
    assert.ok((await evaluate("document.querySelector('#revision-stamp').textContent")).includes('release-one'));
    checks.push('all 28 SQLite-ordered packages page 20+8 with unknowns last and stable pinned revision');
    await evaluate("document.querySelector('#previous-page').click()");await until(ready+" && document.querySelectorAll('.result-card').length===20");assert.deepEqual(await ids(),first);
    retired=true;await evaluate("document.querySelector('#next-page').click()");await until("document.querySelector('#query-error')?.hidden===false");
    assert.deepEqual(await ids(),first);assert.equal(await evaluate("document.querySelector('#next-page').disabled"),true);
    retired=false;await evaluate("document.querySelector('#retry-query').click()");await until(ready+" && document.querySelectorAll('.result-card').length===8");
    checks.push('expired cursor retains the displayed ordered page and exact retry recovers');
    await evaluate("document.querySelector('#query').value='gamma';document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))");await until(ready+" && document.querySelectorAll('.result-card').length===1");
    assert.ok((await ids())[0].includes('plugin.gamma'));
    assert.ok(requests.some(r=>r.route==='/api/v1/packages'&&r.params.q==='gamma'&&r.params.sort==='latest_release'));
    checks.push('text filtering executes in the complete SQLite catalog and retains explicit unknown status');
    slowOnce=true;await evaluate("document.querySelector('#query').value='';document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))");
    await until("document.querySelector('#catalog-status')?.textContent==='正在查询…'");
    await evaluate("document.querySelector('#query').value='gamma';document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))");await until(ready+" && document.querySelectorAll('.result-card').length===1");await timers.setTimeout(1100);
    assert.ok((await ids())[0].includes('plugin.gamma'));
    checks.push('a cancelled slow broad query cannot overwrite the newer filtered ordering');
    await evaluate('ForgeUI.setLocale("en");ForgeUI.setTheme("dark")');await width(390);await fits();
    assert.ok((await evaluate("document.querySelector('#ranking-context').textContent")).includes('Prereleases'));
    await evaluate("document.querySelector('#results').scrollIntoView({block:'start'})");await screenshot('release-sort-mobile-dark.png');await width(320);await fits();
    checks.push('English and dark 390/320px layout display release semantics and unknowns without overflow');
    await evaluate('ForgeUI.setLocale("zh-CN")');await width(1365);await send('Page.navigate',{url:sortUrl},session);await until(ready);
    await evaluate("document.querySelector('#agent-filter').value='dsh';document.querySelector('#type-filter').value='plugin';document.querySelector('#agent-filter').dispatchEvent(new Event('change',{bubbles:true}))");
    await until(ready+" && document.querySelector('#agent-filter').value==='dsh'");
    await until("document.querySelector('#revision-stamp').textContent.includes('release-two')");
    assert.ok(requests.some(r=>r.params.sort==='latest_release'&&r.params.agent==='dsh'&&r.params.type==='plugin'));
    await screenshot('release-sort-desktop.png');checks.push('Agent/type filters remain explicit server query parameters with the release sort');
    oldMode=true;await send('Page.navigate',{url:base+'catalog/?revision=release-one'},session);await until(ready);
    assert.equal(await evaluate("document.querySelector('#sort-select option[value=latest_release]').disabled"),true);
    await send('Page.navigate',{url:sortUrl},session);await until("document.querySelector('#query-error')?.hidden===false");
    assert.ok((await evaluate("document.querySelector('#query-error-message').textContent")).includes('不支持声明发行排序'));
    assert.equal(await evaluate("document.querySelectorAll('.result-card').length"),0);
    checks.push('old manifests disable the option and an explicit unsupported sort never silently changes ordering');
    assert.deepEqual(assetErrors,[]);assert.deepEqual(unmatched,[]);assert.deepEqual(exceptions,[]);
    assert.ok(requests.every(request=>!request.cookie&&!request.authorization));
    assert.ok(!network.some(url=>/\/data\/.*index\.json/.test(url)));
    checks.push('no credentials, full catalog JSON fetches, unmatched fixture requests or runtime exceptions');
  }catch(error){failure=error;if(send&&session){try{const value=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(artifacts,'failure.png'),Buffer.from(value.data,'base64'));}catch(_){}}}
  finally{
    fs.writeFileSync(path.join(artifacts,'release-sort-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,exceptions,unmatched,assetErrors,error:failure?failure.message:null,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
  }
  if(failure)throw failure;
});

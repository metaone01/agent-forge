/* Uses the existing Edge installation and Node CDP; all remote previews are intercepted. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_RELEASE_UI_SITE&&process.env.FORGE_RELEASE_FIXTURE&&process.env.FORGE_RELEASE_UI_ARTIFACTS;

test('Release UI: exact declarations, asset counters, repository context, paging and revision recovery',{skip:!enabled,timeout:120000},async()=>{
  const site=path.resolve(process.env.FORGE_RELEASE_UI_SITE),artifacts=path.resolve(process.env.FORGE_RELEASE_UI_ARTIFACTS);
  fs.mkdirSync(artifacts,{recursive:true});
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_RELEASE_FIXTURE,'utf8'));
  const key=(revision,route,params)=>JSON.stringify([revision,route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(item=>[key(item.revision,item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],assetErrors=[],checks=[];
  let current='release-one',failOnce=true,retired=false,slowOnce=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length)),params=Object.fromEntries(url.searchParams);
      requests.push({route,params,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      const revision=params.revision||current;
      let value=cases.get(key(revision,route,params));
      if(route.endsWith('/releases')&&failOnce){failOnce=false;value={status:503,body:{error:{code:'service_busy'}}};}
      if(route.endsWith('/releases')&&params.revision==='release-one'&&retired)value={status:410,body:{error:{code:'cursor_expired'}}};
      if(!value){unmatched.push({route,params,revision});res.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({error:{code:'fixture_missing'}}));return;}
      const reply=()=>{if(!res.destroyed)res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));};
      if(route.endsWith('/releases')&&slowOnce){slowOnce=false;setTimeout(reply,1000);}else reply();return;
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
    const ready="document.querySelectorAll('.release-item').length>0 && !document.querySelector('.release-panel [role=alert]') && !document.querySelector('.release-panel [role=status]')";
    const detail=(id='plugin.alpha',revision='release-one',version='2.0.0')=>base+'catalog/#/id/'+encodeURIComponent(id)+'?revision='+revision+'&version='+encodeURIComponent(version);
    const open=async()=>{await until("document.querySelector('.release-panel > details')");await evaluate("document.querySelector('.release-panel > details').open=true");};
    const identities=()=>evaluate("Array.from(document.querySelectorAll('.release-item > .release-association code')).map(node=>node.textContent)");
    await width(1365);await send('Page.navigate',{url:detail()},session);
    await until("document.querySelector('#canonical-version') && document.querySelector('.release-panel > details')");
    assert.equal(requests.filter(request=>request.route.endsWith('/releases')).length,0);
    await open();await until("document.querySelector('[data-release-retry]') && !document.querySelector('[data-release-retry]').disabled");
    await evaluate("document.querySelector('[data-release-retry]').click()");await until(ready);
    assert.equal(await evaluate("document.querySelectorAll('.release-item').length"),1);
    assert.ok((await evaluate("document.querySelector('.release-association').textContent")).includes('关联到规范分发声明'));
    assert.ok((await evaluate("document.querySelector('.release-scope').textContent")).includes('2.0.0'));
    assert.ok((await evaluate("document.querySelector('.release-association').textContent")).includes('v1.0.0'));
    assert.equal(await evaluate('window.__releaseInjected'),undefined);
    assert.ok((await evaluate("document.querySelector('.release-item h3').textContent")).includes('<script>'));
    assert.ok((await evaluate("document.querySelector('.release-notice').textContent")).includes('不是使用人数'));
    assert.equal(await evaluate("document.querySelector('.release-assets tbody td:nth-child(2)').textContent"),'0');
    await evaluate("document.querySelector('.release-panel').scrollIntoView({block:'start'})");await screenshot('release-declared-desktop.png');
    checks.push('lazy exact-tag query retries 503, keeps canonical 2.0.0 separate from tag v1.0.0, escapes prose and preserves zero asset requests');
    await evaluate("document.querySelector('#canonical-version').value='1.0.0';document.querySelector('#canonical-version').dispatchEvent(new Event('change',{bubbles:true}))");
    await until("document.querySelector('#canonical-version')?.value==='1.0.0'");await open();await until(ready+" && document.querySelectorAll('.release-item').length===20");
    assert.ok((await evaluate("document.querySelector('.release-association').textContent")).includes('仅所属仓库上下文'));
    const first=await identities();assert.equal(new Set(first).size,20);
    current='release-two';await evaluate("document.querySelector('[data-release-next]').click()");await until(ready+" && document.querySelectorAll('.release-item').length===3");
    const tail=await identities();assert.equal(new Set([...first,...tail]).size,23);
    assert.ok((await evaluate("document.querySelector('.release-scope').textContent")).includes('release-one'));
    await evaluate("document.querySelector('[data-release-prev]').click()");await until(ready+" && document.querySelectorAll('.release-item').length===20");
    assert.deepEqual(await identities(),first);
    checks.push('historical canonical version shows repository context only; all 23 observed releases page 20+3 without switching revision');
    await evaluate('ForgeUI.setLocale("en");ForgeUI.setTheme("dark")');await width(390);
    await evaluate("document.querySelector('.release-panel').scrollIntoView({block:'start'})");await fits();
    assert.ok(await evaluate("document.querySelector('.release-panel > details > summary').getBoundingClientRect().top>=document.querySelector('.topbar').getBoundingClientRect().bottom"));
    assert.ok((await evaluate("document.querySelector('.release-notice').textContent")).includes('not people or npm downloads'));
    await screenshot('release-context-mobile-dark.png');await width(320);await fits();
    checks.push('English and dark theme keep metadata boundaries and the complete panel heading visible at 390/320px');
    await width(1365);await send('Page.navigate',{url:detail('plugin.beta','release-two','1.0.0')},session);await open();await until(ready);
    assert.equal(await evaluate("document.querySelector('.release-assets tbody td:nth-child(2)').textContent"),'200');
    assert.ok((await evaluate("document.querySelector('.release-item details').textContent")).includes('Exact asset declaration'));
    assert.ok(requests.some(request=>request.route==='/api/v1/packages/plugin.beta/releases'&&request.params.version==='1.0.0'&&request.params.revision==='release-two'));
    checks.push('exact asset declaration is linked by asset name/ID and exposes source cumulative requests without npm aggregation');
    await send('Page.navigate',{url:detail('plugin.gamma','release-two','1.0.0')},session);await open();
    await until("document.querySelector('.release-panel').textContent.includes('No unambiguous release observation') && !document.querySelector('.release-panel [role=status]')");
    assert.equal(await evaluate("document.querySelectorAll('.release-item').length"),0);
    checks.push('similar tag text without the declared exact URL stays unknown and is not guessed from the package version');
    await send('Page.navigate',{url:detail('plugin.alpha','release-one','1.0.0')},session);await open();await until(ready);
    retired=true;await evaluate("document.querySelector('[data-release-next]').click()");
    await until("document.querySelector('.release-panel [role=alert]')?.textContent.includes('retired')");
    assert.equal(await evaluate("document.querySelectorAll('.release-item').length"),20);assert.equal(await evaluate("document.querySelector('[data-release-next]').disabled"),true);
    checks.push('retired cursor preserves old evidence and disables forward paging without silently switching revisions');
    retired=false;await send('Page.navigate',{url:detail('plugin.alpha','release-one','1.0.0')},session);
    await until("document.querySelector('#canonical-version')?.value==='1.0.0'");slowOnce=true;await open();
    await until("document.querySelector('.release-panel [role=status]')");
    await evaluate("location.hash='#/id/plugin.beta?revision=release-two&version=1.0.0'");
    await until("document.querySelector('h1')?.textContent.includes('beta') && document.querySelector('.release-panel > details')");await open();await until(ready);
    await timers.setTimeout(1100);
    assert.ok((await evaluate("document.querySelector('.release-scope').textContent")).includes('release-two'));
    assert.equal(await evaluate("document.querySelector('.release-assets tbody td:nth-child(2)').textContent"),'200');
    checks.push('cancelled slow old-version response cannot overwrite a new package/revision panel');
    assert.deepEqual(assetErrors,[]);assert.deepEqual(unmatched,[]);assert.deepEqual(exceptions,[]);
    assert.ok(requests.every(request=>!request.cookie&&!request.authorization));
    assert.ok(!network.some(url=>/\/data\/.*index\.json/.test(url)));
    const ownBadges=['validate','project','release'].map(name=>'https://github.com/metaone01/agent-forge/actions/workflows/'+name+'.yml/badge.svg');
    const external=network.filter(url=>!ownBadges.includes(url)&&!url.startsWith(base)&&!url.startsWith('https://media.example.test/')&&!url.includes('me.kis.v2.scr.kaspersky-labs.com')&&!url.startsWith('http://127.0.0.1:'));
    assert.deepEqual(external,[]);
    checks.push('no private API credentials, unexpected network, asset errors, runtime exceptions or unmatched fixtures');
  }catch(error){failure=error;if(send&&session){try{const value=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(artifacts,'failure.png'),Buffer.from(value.data,'base64'));}catch(_){}}}
  finally{
    fs.writeFileSync(path.join(artifacts,'release-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,exceptions,unmatched,assetErrors,error:failure?failure.message:null,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
  }
  if(failure)throw failure;
});

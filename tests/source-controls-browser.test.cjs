/* Uses the existing Edge installation and Node CDP; all remote previews are intercepted. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_CONTROL_UI_SITE&&process.env.FORGE_CONTROL_UI_FIXTURE&&process.env.FORGE_CONTROL_UI_ARTIFACTS;

test('collection controls UI: lazy GET, retry, full snapshot paging, history and pinned revisions',{skip:!enabled,timeout:120000},async()=>{
  const site=path.resolve(process.env.FORGE_CONTROL_UI_SITE),artifacts=path.resolve(process.env.FORGE_CONTROL_UI_ARTIFACTS);
  fs.mkdirSync(artifacts,{recursive:true});
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_CONTROL_UI_FIXTURE,'utf8'));
  const key=(revision,route,params)=>JSON.stringify([revision,route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(item=>[key(item.revision,item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],assetErrors=[],checks=[];
  let current='controls-one',failOnce=true,retired=false,slowOnce=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length)),params=Object.fromEntries(url.searchParams);
      requests.push({route,params,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      const revision=params.revision||current;
      let value=cases.get(key(revision,route,params));
      if(route==='/api/v1/source-controls'&&failOnce){failOnce=false;value={status:503,body:{error:{code:'service_busy'}}};}
      if(route==='/api/v1/source-controls'&&params.revision==='controls-one'&&retired)value={status:410,body:{error:{code:'cursor_expired'}}};
      if(!value){unmatched.push({route,params,revision});res.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({error:{code:'fixture_missing'}}));return;}
      const reply=()=>{if(!res.destroyed)res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));};
      if(route==='/api/v1/source-controls'&&slowOnce){slowOnce=false;setTimeout(reply,1000);}else reply();return;
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
    const items=()=>evaluate("Array.from(document.querySelectorAll('.control-item > code')).map(node=>node.textContent)");
    const ready="document.querySelectorAll('.control-item').length>0 && !document.querySelector('.control-error')";
    await width(1365);await send('Page.navigate',{url:base+'catalog/?revision=controls-one'},session);
    await until("document.querySelectorAll('.result-card').length===2 && !document.querySelector('#source-control-panel').hidden");
    assert.equal(requests.filter(request=>request.route==='/api/v1/source-controls').length,0);
    assert.ok((await evaluate("document.querySelector('#source-control-panel').textContent")).includes('23'));
    checks.push('collapsed panel discloses scope totals without querying controls or altering indexed results');
    await evaluate("document.querySelector('#source-control-panel > details').open=true");
    await until("document.querySelector('.control-error') && !document.querySelector('[data-control-retry]').disabled");
    await evaluate("document.querySelector('[data-control-retry]').click()");await until(ready+" && document.querySelectorAll('.control-item').length===20");
    const first=await items();assert.equal(new Set(first).size,20);
    assert.ok((await evaluate("document.querySelector('.control-notice').textContent")).includes('不删除'));
    assert.equal(await evaluate("document.querySelectorAll('#source-control-panel [data-source-control-write]').length"),0);
    assert.ok(!(await evaluate("document.querySelector('#source-control-panel').textContent")).includes('private-control-operator'));
    checks.push('503 retry preserves catalog and shows factual, read-only control records with private labels omitted');
    current='controls-two';await evaluate("document.querySelector('[data-control-next]').click()");await until(ready+" && document.querySelectorAll('.control-item').length===3");
    const tail=await items();assert.equal(new Set([...first,...tail]).size,23);
    assert.ok((await evaluate("document.querySelector('.control-revision').textContent")).includes('controls-one'));
    assert.equal(await evaluate("document.querySelector('[data-control-next]').disabled"),true);
    await evaluate("document.querySelector('[data-control-prev]').click()");await until(ready+" && document.querySelectorAll('.control-item').length===20");
    assert.deepEqual(await items(),first);
    checks.push('all 23 targets appear once across 20+3 pages pinned to the old revision after publication');
    const historyIndex=first.indexOf(fixture.historyTarget);
    if(historyIndex>=0){
      await evaluate(`document.querySelectorAll('.control-item')[${historyIndex}].querySelector('details').open=true`);
      assert.equal(await evaluate(`document.querySelectorAll('.control-item')[${historyIndex}].querySelectorAll('li').length`),20);
      assert.ok((await evaluate(`document.querySelectorAll('.control-item')[${historyIndex}].textContent`)).includes('22'));
    }else{
      await evaluate("document.querySelector('[data-control-next]').click()");await until(ready+" && document.querySelectorAll('.control-item').length===3");
      const index=tail.indexOf(fixture.historyTarget);assert.ok(index>=0);
      await evaluate(`document.querySelectorAll('.control-item')[${index}].querySelector('details').open=true`);
      assert.equal(await evaluate(`document.querySelectorAll('.control-item')[${index}].querySelectorAll('li').length`),20);
      assert.ok((await evaluate(`document.querySelectorAll('.control-item')[${index}].textContent`)).includes('22'));
      await evaluate("document.querySelector('[data-control-prev]').click()");await until(ready+" && document.querySelectorAll('.control-item').length===20");
    }
    checks.push('22-event history retains a disclosed latest-20 display without claiming operator authentication');
    await screenshot('controls-desktop.png');
    await evaluate("document.querySelector('[data-control-latest]').click()");await until(ready+" && document.querySelector('.control-revision').textContent.includes('controls-two')");
    assert.ok((await evaluate("document.querySelector('#source-control-panel > details > summary').textContent")).includes('20'));
    assert.ok((await evaluate("document.querySelector('#revision-stamp').textContent")).includes('controls-one'));
    checks.push('explicit latest controls select new state while the panel states its separate revision');
    await evaluate('ForgeUI.setTheme("dark");ForgeUI.setLocale("en")');await width(390);await fits();
    assert.ok((await evaluate("document.querySelector('.control-revision').textContent")).includes('controls-two'));
    await evaluate("document.querySelector('#source-control-panel').scrollIntoView({block:'start'})");
    assert.ok(await evaluate("document.querySelector('#source-control-panel > details > summary').getBoundingClientRect().top>=document.querySelector('.topbar').getBoundingClientRect().bottom"));await screenshot('controls-mobile-dark.png');
    await width(320);await fits();
    assert.ok((await evaluate("document.querySelector('.control-notice').textContent")).includes('resuming one scope'));
    assert.ok(!(await evaluate("document.querySelector('#source-control-panel').textContent")).includes('采集控制记录'));
    checks.push('English, dark theme and 390/320px layouts keep scope precedence and revision visible');
    await width(1365);await send('Page.navigate',{url:base+'catalog/?revision=controls-one'},session);
    await until("document.querySelectorAll('.result-card').length===2 && !document.querySelector('#source-control-panel').hidden");
    await evaluate("document.querySelector('#source-control-panel > details').open=true");await until(ready);
    retired=true;await evaluate("document.querySelector('[data-control-next]').click()");
    await until("document.querySelector('.control-error')?.textContent.includes('retired')");
    assert.equal((await items()).length,20);assert.equal(await evaluate("document.querySelector('[data-control-next]').disabled"),true);
    await evaluate("document.querySelector('[data-control-latest]').click()");await until(ready+" && document.querySelector('.control-revision').textContent.includes('controls-two')");
    checks.push('retired cursor retains old entries and explicit latest recovers without inferred state');
    retired=false;
    await send('Page.navigate',{url:base+'catalog/?revision=controls-one'},session);
    await until("document.querySelectorAll('.result-card').length===2 && !document.querySelector('#source-control-panel').hidden");
    slowOnce=true;await evaluate("document.querySelector('#source-control-panel > details').open=true");
    await until("document.querySelector('#source-control-panel [role=status]')");
    await evaluate("location.hash='#/id/plugin.alpha?revision=controls-two'");
    await until("document.querySelector('#canonical-version') && document.querySelector('.control-revision').textContent.includes('controls-two') && document.querySelectorAll('.control-item').length===20");
    await timers.setTimeout(1100);
    assert.ok((await evaluate("document.querySelector('.control-revision').textContent")).includes('controls-two'));
    checks.push('slow old-control request cannot replace the new detail revision after navigation');
    await evaluate("location.hash='#/id/plugin.alpha?revision=controls-two'");
    await until("document.querySelector('#canonical-version') && document.querySelector('.control-revision').textContent.includes('controls-two')");
    assert.equal(await evaluate("document.querySelector('#source-control-panel').hidden"),false);
    checks.push('independent control panel remains available on the exact canonical detail revision');
    await send('Page.navigate',{url:base+'catalog/?revision=controls-legacy'},session);
    await until("document.querySelectorAll('.result-card').length===2 && document.querySelector('#source-control-panel').hidden");
    assert.equal(await evaluate("document.querySelector('#source-control-panel').textContent"),'');
    checks.push('legacy feature absence hides the optional panel while canonical query remains usable');
    assert.deepEqual(assetErrors,[]);assert.deepEqual(unmatched,[]);assert.deepEqual(exceptions,[]);
    assert.ok(requests.every(request=>!request.cookie&&!request.authorization));
    assert.ok(!network.some(url=>/\/data\/.*index\.json/.test(url)));
    const ownBadges=['validate','project','release'].map(name=>'https://github.com/metaone01/agent-forge/actions/workflows/'+name+'.yml/badge.svg');
    const external=network.filter(url=>!ownBadges.includes(url)&&!url.startsWith(base)&&!url.startsWith('https://media.example.test/')&&!url.includes('me.kis.v2.scr.kaspersky-labs.com')&&!url.startsWith('http://127.0.0.1:'));
    assert.deepEqual(external,[]);
    checks.push('no private API credentials, unexpected network, asset errors, runtime exceptions or unmatched fixtures');
  }catch(error){failure=error;if(send&&session){try{const value=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(artifacts,'failure.png'),Buffer.from(value.data,'base64'));}catch(_){}}}
  finally{
    fs.writeFileSync(path.join(artifacts,'source-controls-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,exceptions,unmatched,assetErrors,error:failure?failure.message:null,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
  }
  if(failure)throw failure;
});

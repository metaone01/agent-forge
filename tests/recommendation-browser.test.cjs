/* Real Edge acceptance against SQLite-generated, read-only response fixtures. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_RECOMMENDATION_UI_FIXTURE&&process.env.FORGE_RECOMMENDATION_UI_ARTIFACTS;

test('curated UI: exact version, source browsing, withdrawal, retry and responsive rendering', {skip:!enabled,timeout:110000}, async()=>{
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_RECOMMENDATION_UI_FIXTURE,'utf8'));
  const artifacts=path.resolve(process.env.FORGE_RECOMMENDATION_UI_ARTIFACTS);fs.mkdirSync(artifacts,{recursive:true});
  const site=process.env.FORGE_RECOMMENDATION_UI_SITE?path.resolve(process.env.FORGE_RECOMMENDATION_UI_SITE):path.resolve(__dirname,'../site');
  const key=(revision,route,params)=>JSON.stringify([revision,route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(c=>[key(c.revision,c.path,c.parameters),c]));
  const requests=[],network=[],networkEvents=[],networkFailures=[],environmentBlockedRequests=[],exceptions=[],unmatched=[],checks=[];
  let current='rec-one',retired=false,failOnce=false,delayOnce=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length));
      const params=Object.fromEntries(url.searchParams),revision=params.revision||current;
      requests.push({route,params,revision,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      let value=cases.get(key(revision,route,params));
      if(retired&&params.revision==='rec-one')value={status:410,body:{error:{code:params.cursor?'cursor_expired':'revision_unavailable'},requestId:'retired'}};
      if(route.endsWith('/recommendations')&&failOnce){failOnce=false;value={status:503,body:{error:{code:'service_busy'},requestId:'retry'}};}
      if(!value){unmatched.push({route,params,revision});value={status:503,body:{error:{code:'fixture_unmatched'},requestId:'unmatched'}};}
      const respond=()=>{if(!res.destroyed)res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));};
      if(route.endsWith('/recommendations')&&delayOnce){delayOnce=false;setTimeout(respond,1100);}else respond();return;
    }
    let route;try{route=decodeURIComponent(url.pathname).replace(/^\/project\//,'');}catch(_){res.writeHead(400).end();return;}
    const file=path.resolve(site,route.endsWith('/')?route+'index.html':route);
    if(!file.startsWith(site+path.sep)){res.writeHead(403).end();return;}
    try{res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html','Set-Cookie':'fixture=present; Path=/'}).end(fs.readFileSync(file));}catch(_){res.writeHead(404).end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port+'/project/';
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'forge-recommendation-browser-'));
  const browser=spawn(process.env.FORGE_UI_BROWSER,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket,send,session,failure,diagnostics;
  try{
    let debug;
    for(let attempt=0;attempt<100;attempt++){try{debug=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split(/\r?\n/);break;}catch(_){await timers.setTimeout(100);}}
    assert.ok(debug,'isolated browser exposes debugging port');
    socket=new WebSocket('ws://127.0.0.1:'+debug[0]+debug[1]);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    let sequence=0;const pending=new Map();
    socket.addEventListener('message',event=>{
      const message=JSON.parse(event.data);
      if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);
      if(message.method==='Network.requestWillBeSent'){network.push(message.params.request.url);networkEvents.push(message.params);}
      if(message.method==='Network.loadingFailed')networkFailures.push(message.params);
      if(message.id&&pending.has(message.id)){const item=pending.get(message.id);pending.delete(message.id);clearTimeout(item.timer);message.error?item.reject(new Error(JSON.stringify(message.error))):item.resolve(message.result);}
    });
    send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++sequence;const timer=setTimeout(()=>{pending.delete(id);reject(new Error('CDP timed out '+method));},10000);pending.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
    const target=await send('Target.createTarget',{url:'about:blank'});session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
    await send('Page.enable',{},session);await send('Runtime.enable',{},session);await send('Network.enable',{},session);
    // Block only the verified host-antivirus injection in this isolated browser target.
    await send('Network.setBlockedURLs',{urls:['*://me.kis.v2.scr.kaspersky-labs.com/*']},session);
    const evaluate=async expression=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const until=async expression=>{const started=Date.now();while(Date.now()-started<8000){if(await evaluate(expression))return;await timers.setTimeout(50);}throw new Error('Timed out: '+expression);};
    const screenshot=async name=>{const result=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);fs.writeFileSync(path.join(artifacts,name),Buffer.from(result.data,'base64'));};
    const select=async(id,value)=>evaluate(`document.getElementById(${JSON.stringify(id)}).value=${JSON.stringify(value)};document.getElementById(${JSON.stringify(id)}).dispatchEvent(new Event('change',{bubbles:true}))`);
    const search=async value=>evaluate(`document.querySelector('#query').value=${JSON.stringify(value)};document.querySelector('#query').dispatchEvent(new Event('input',{bubbles:true}))`);
    const idle="document.querySelector('#results')?.getAttribute('aria-busy')==='false' && document.querySelector('#query-error').hidden";
    const change=async(id,value)=>evaluate(`document.getElementById(${JSON.stringify(id)}).value=${JSON.stringify(value)};document.getElementById(${JSON.stringify(id)}).dispatchEvent(new Event('change',{bubbles:true}))`);
    const navigate=async hash=>send('Page.navigate',{url:base+'catalog/?revision=rec-one'+hash},session);
    const packageLink=(revision,version)=>'#/id/'+encodeURIComponent(fixture.packageId)+'?revision='+revision+'&version='+version;
    const recIdle="document.querySelector('.rec-results')?.getAttribute('aria-busy')==='false'";
    await send('Emulation.setDeviceMetricsOverride',{width:1365,height:950,deviceScaleFactor:1,mobile:false},session);
    await navigate(packageLink('rec-one','1.0.0'));
    await until(recIdle+" && document.querySelector('.rec-item')");
    assert.ok((await evaluate("document.querySelector('.rec-panel').textContent")).includes('1.0.0'));
    assert.ok((await evaluate("document.querySelector('.rec-panel').textContent")).includes('不推断插件背书'));
    assert.equal(await evaluate("document.querySelector('.rec-item').dataset.membership"),'active');
    assert.equal(await evaluate('window.__injected'),undefined);
    assert.equal(await evaluate("document.querySelector('.rec-item img')"),null);
    await evaluate("document.querySelector('.rec-citations').open=true;document.querySelector('.rec-proof').open=true");
    assert.ok((await evaluate("document.querySelector('.rec-citations').textContent")).includes('另有 5 条'));
    assert.ok((await evaluate("document.querySelector('.rec-proof').textContent")).includes('SHA-256'));
    assert.equal(await evaluate("getComputedStyle(document.querySelector('#rec-retry')).display"),'none');
    await evaluate("document.querySelector('.rec-panel').scrollIntoView()");await screenshot('exact-version-desktop.png');
    checks.push('exact canonical version/hash, escaped names, bounded citations and fixed evidence');
    await change('canonical-version','2.0.0');
    await until(recIdle+" && document.querySelector('.rec-empty') && document.querySelector('#canonical-version').value==='2.0.0'");
    assert.equal(await evaluate("document.querySelectorAll('.rec-item').length"),0);
    assert.ok((await evaluate("document.querySelector('.rec-empty').textContent")).includes('不能据此判断全网没有推荐'));
    checks.push('selected version changes association without canonical latest fallback');
    delayOnce=true;await evaluate(`location.hash=${JSON.stringify(packageLink('rec-one','1.0.0'))}`);
    await until("document.querySelector('.rec-results')?.getAttribute('aria-busy')==='true'");
    await evaluate(`location.hash=${JSON.stringify(packageLink('rec-one','2.0.0'))}`);
    await until(recIdle+" && document.querySelector('.rec-empty') && document.querySelector('#canonical-version').value==='2.0.0'");
    await timers.setTimeout(1200);
    assert.equal(await evaluate("document.querySelectorAll('.rec-item').length"),0);
    checks.push('late response from canceled historical version cannot overwrite new version');
    await navigate('#/recommendations?revision=rec-one');
    await until(recIdle+" && document.querySelector('[data-source]')");
    assert.equal(await evaluate("document.querySelector('[data-source]').dataset.source"),fixture.sourceId);
    await evaluate("document.querySelector('[data-source] h3 a').click()");
    await until(recIdle+" && document.querySelectorAll('[data-target]').length===20");
    const first=await evaluate("[...document.querySelectorAll('[data-target]')].map(node=>node.dataset.target)");
    current='rec-two';await evaluate("document.querySelector('#rec-next').click()");
    await until(recIdle+" && document.querySelectorAll('[data-target]').length===12");
    const second=await evaluate("[...document.querySelectorAll('[data-target]')].map(node=>node.dataset.target)");
    assert.equal(new Set([...first,...second]).size,32);
    assert.ok((await evaluate("document.querySelector('.rec-revision').textContent")).includes('rec-one'));
    await evaluate("document.querySelector('#rec-prev').click()");await until(recIdle+" && document.querySelectorAll('[data-target]').length===20");
    assert.deepEqual(await evaluate("[...document.querySelectorAll('[data-target]')].map(node=>node.dataset.target)"),first);
    checks.push('source browsing and 20+12 member pagination stay pinned across current revision switch');
    await change('rec-status','withdrawn');
    await until(recIdle+" && document.querySelector('.rec-empty')");
    assert.equal(await evaluate("document.querySelectorAll('[data-target]').length"),0);
    checks.push('empty filtered scope is unknown, with no invented rank');
    await navigate(packageLink('rec-two','1.0.0'));await until(recIdle+" && document.querySelector('[data-membership=withdrawn]')");
    assert.equal(await evaluate("document.querySelectorAll('.rec-proof').length"),2);
    assert.ok((await evaluate("document.querySelector('.rec-freshness').textContent")).includes('已过期'));
    checks.push('withdrawal retains before/after proof and stale membership facts');
    failOnce=true;await change('rec-status','active');
    await until("document.querySelector('#rec-retry') && !document.querySelector('#rec-retry').hidden");
    await evaluate("document.querySelector('#rec-retry').click()");await until(recIdle+" && document.querySelector('.rec-empty') && document.querySelector('#rec-retry').hidden");
    checks.push('failed recommendation request retries independently of detail/versions');
    await evaluate("window.ForgeUI.setLocale('en');window.ForgeUI.setTheme('dark')");
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
    await until("document.documentElement.lang==='en' && document.querySelector('.rec-panel').textContent.includes('Source curated lists')");
    assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth+1'),true);
    await evaluate("document.querySelector('.rec-panel').scrollIntoView()");
    assert.equal(await evaluate("(()=>{const nodes=[...document.querySelector('.rec-pagination').children];return nodes.every((a,i)=>nodes.slice(i+1).every(b=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.right<=y.left||y.right<=x.left||x.bottom<=y.top||y.bottom<=x.top}))})()"),true);
    await screenshot('english-dark-mobile.png');
    checks.push('English/dark/mobile rendering stays within viewport');
    await navigate('#/recommendations/'+encodeURIComponent(fixture.sourceId)+'?revision=rec-one');
    await until(recIdle+" && document.querySelectorAll('[data-target]').length===20");
    retired=true;await evaluate("document.querySelector('#rec-next').click()");
    await until("document.querySelector('.rec-latest') && document.querySelector('#rec-retry') && !document.querySelector('#rec-retry').hidden");
    assert.equal(await evaluate("document.querySelectorAll('[data-target]').length"),20);
    assert.ok((await evaluate("document.querySelector('.rec-pagination output').textContent")).includes('1'));
    assert.equal(await evaluate("document.querySelector('#rec-next').disabled"),true);
    await evaluate("document.querySelector('.rec-latest').click()");await until(recIdle+" && document.querySelector('.rec-revision').textContent.includes('rec-two')");
    assert.ok((await evaluate("document.querySelector('main').textContent")).includes('Upstream rank: unknown'));
    checks.push('retired cursor preserves last results and offers explicit latest source refresh');
    assert.equal(unmatched.length,0,JSON.stringify(unmatched));assert.equal(exceptions.length,0,JSON.stringify(exceptions));
    assert.equal(requests.some(r=>r.cookie||r.authorization),false);
    assert.equal(network.filter(url=>url.includes('/data/')).length,0);
    for(const event of networkEvents){
      const url=new URL(event.request.url);
      if(url.hostname==='me.kis.v2.scr.kaspersky-labs.com' && /^\/[A-F0-9-]+\/main\.js$/.test(url.pathname) && event.type==='Script' && event.initiator.type==='parser' && event.initiator.url.startsWith(base) && networkFailures.some(item=>item.requestId===event.requestId&&item.blockedReason==='inspector'))environmentBlockedRequests.push({url:event.request.url,requestId:event.requestId,initiator:event.initiator,blockedReason:'inspector'});
    }
    const environmentUrls=new Set(environmentBlockedRequests.map(item=>item.url));
    assert.equal(network.filter(url=>!url.startsWith(base)&&url!==new URL('/favicon.ico',base).href&&!url.startsWith('about:')&&!url.startsWith('data:')&&!environmentUrls.has(url)).length,0);
    checks.push('no catalog JSON downloads, no external media/plugin requests, omitted credentials and no runtime errors');
  }catch(error){
    failure=error;
    if(send&&session)try{diagnostics=await send('Runtime.evaluate',{expression:"JSON.stringify({url:location.href,html:document.querySelector('main')?.innerHTML})",returnByValue:true},session);}catch(_){}
    throw error;
  }finally{
    fs.writeFileSync(path.join(artifacts,'recommendation-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,networkFailures,environmentBlockedRequests,exceptions,unmatched,diagnostics,error:failure&&failure.message,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(send)try{await send('Browser.close');}catch(_){}
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
    const resolved=path.resolve(profile),tempRoot=path.resolve(os.tmpdir());
    assert.ok(resolved.startsWith(tempRoot+path.sep)&&path.basename(resolved).startsWith('forge-recommendation-browser-'));
    for(let attempt=0;attempt<6;attempt++){try{fs.rmSync(resolved,{recursive:true,force:true});break;}catch(error){if(attempt===5){if(!failure)throw error;}else await timers.setTimeout(100*(attempt+1));}}
  }
});

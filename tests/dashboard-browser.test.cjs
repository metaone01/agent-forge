/* Live Edge/Chromium UI acceptance with existing tools and SQLite-generated DTO fixtures.
 * FORGE_UI_BROWSER=<installed executable> FORGE_DASHBOARD_FIXTURE=<json> FORGE_DASHBOARD_ARTIFACTS=<directory>
 * node --test tests/dashboard-browser.test.cjs
 * This local test server does not validate or replace the FastAPI adapter.
 */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const { spawn } = require('node:child_process');
const timers = require('node:timers/promises');
const enabled = process.env.FORGE_UI_BROWSER && process.env.FORGE_DASHBOARD_FIXTURE && process.env.FORGE_DASHBOARD_ARTIFACTS;

test('Public dashboard in a real browser: revision links, failure recovery, declarations and catalog integration', { skip: !enabled, timeout: 90000 }, async () => {
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_DASHBOARD_FIXTURE,'utf8'));
  const artifacts=path.resolve(process.env.FORGE_DASHBOARD_ARTIFACTS);
  fs.mkdirSync(artifacts,{recursive:true});
  const site=process.env.FORGE_DASHBOARD_SITE ? path.resolve(process.env.FORGE_DASHBOARD_SITE) : path.resolve(__dirname,'../site');
  const key=(revision,route,parameters)=>JSON.stringify([revision,route,Object.entries(parameters).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map((item)=>[key(item.revision,item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],checks=[];
  let current='browser-one',retired=false,failDashboardOnce=false;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length));
      const parameters=Object.fromEntries(url.searchParams);
      const revision=parameters.revision||current;
      let record=cases.get(key(revision,route,parameters));
      if(retired&&parameters.revision==='browser-one')record=cases.get(key(current,'/errors/revision_unavailable',{}));
       if(route==='/api/v1/dashboard'&&failDashboardOnce){failDashboardOnce=false;record=cases.get(key(current,'/errors/service_busy',{}));}
      requests.push({route,parameters,status:record&&record.status,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      if(!record){unmatched.push({route,parameters,revision});res.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({error:{code:'fixture_missing'},requestId:'fixture'}));return;}
      const send=()=>{if(!res.destroyed)res.writeHead(record.status,{'Content-Type':'application/json','Cache-Control':'no-store'}).end(JSON.stringify(record.body));};
      if(parameters.q==='slow')setTimeout(send,1400);else send();
      return;
    }
    let route;
    try{route=decodeURIComponent(url.pathname).replace(/^\/project\//,'');}catch(_){res.writeHead(400).end();return;}
    const file=path.resolve(site,route.endsWith('/')?route+'index.html':route);
    if(!file.startsWith(site+path.sep)){res.writeHead(403).end();return;}
    try{res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html','Set-Cookie':'fixture_cookie=present; Path=/'}).end(fs.readFileSync(file));}catch(_){res.writeHead(404).end();}
  });
  await new Promise((resolve)=>server.listen(0,'127.0.0.1',resolve));
  const base='http://127.0.0.1:'+server.address().port+'/project/';
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'forge-query-browser-'));
  const browser=spawn(process.env.FORGE_UI_BROWSER,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-component-update','--disable-sync','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket,send,session,failure,diagnostics=null;
  try{
    let debug;
    for(let attempt=0;attempt<100;attempt++){try{debug=fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').trim().split(/\r?\n/);break;}catch(_){await timers.setTimeout(100);}}
    assert.ok(debug,'Browser must expose its own debugging port');
    socket=new WebSocket('ws://127.0.0.1:'+debug[0]+debug[1]);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    let sequence=0;const pending=new Map();
    socket.addEventListener('message',(event)=>{
      const message=JSON.parse(event.data);
      if(message.method==='Runtime.exceptionThrown')exceptions.push(message.params.exceptionDetails);
      if(message.method==='Network.requestWillBeSent')network.push(message.params.request.url);
      if(message.method==='Fetch.requestPaused'){
        send('Fetch.fulfillRequest',{requestId:message.params.requestId,responseCode:200,responseHeaders:[{name:'Content-Type',value:'image/png'}],body:'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6V8AAAAASUVORK5CYII='},message.sessionId).catch(()=>{});
      }
      if(message.id&&pending.has(message.id)){const item=pending.get(message.id);pending.delete(message.id);message.error?item.reject(new Error(JSON.stringify(message.error))):item.resolve(message.result);}
    });
    send=(method,params={},sessionId)=>new Promise((resolve,reject)=>{const id=++sequence;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params,...(sessionId?{sessionId}:{})}));});
    const target=await send('Target.createTarget',{url:'about:blank'});
    session=(await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
    await send('Page.enable',{},session);await send('Runtime.enable',{},session);await send('Network.enable',{},session);
    // Prevent the verified host-antivirus parser injection only in this isolated target.
    await send('Network.setBlockedURLs',{urls:['*://me.kis.v2.scr.kaspersky-labs.com/*']},session);
    await send('Fetch.enable',{patterns:[{urlPattern:'https://media.example.test/*'}]},session);
    const evaluate=async(expression)=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const until=async(expression)=>{const start=Date.now();while(Date.now()-start<7000){if(await evaluate(expression))return;await timers.setTimeout(50);}throw new Error('Timed out: '+expression);};
    const screenshot=async(name)=>{const result=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);fs.writeFileSync(path.join(artifacts,name),Buffer.from(result.data,'base64'));};
    await send('Emulation.setDeviceMetricsOverride',{width:1365,height:900,deviceScaleFactor:1,mobile:false},session);
    const dashboardUrl=base+'catalog/dashboard/';
    await send('Page.navigate',{url:dashboardUrl},session);
    await until('document.querySelector("#dashboard-updated")?.textContent.includes("browser-one")');
    const metrics=()=>evaluate('[...document.querySelectorAll(".metric-value")].map(node=>Number(node.textContent.replaceAll(",","")))');
    assert.deepEqual(await metrics(),[fixture.expected['browser-one'].agents,fixture.expected['browser-one'].packages,fixture.expected['browser-one'].versions,0]);
    assert.ok((await evaluate('document.querySelector("#facet-chart").textContent')).includes('gui.observe'));
    assert.ok((await evaluate('document.querySelector("#custom-facet-chart").textContent')).includes('gui.observe'));
    assert.equal(await evaluate('window.__injected'),undefined);
    assert.ok(await evaluate('[...document.querySelectorAll("#type-bars .bar-row")].filter(row=>row.querySelector(".bar-value").textContent === "0").every(row=>row.querySelector(".bar-fill").style.width === "0%")'));
    assert.ok((await evaluate('document.querySelector("#recent-packages").textContent')).includes('<script>'));
    checks.push('same-revision totals, overlapping Agent coverage, independent facets and escaped metadata');
    const agentHref=await evaluate('[...document.querySelectorAll(".agent-card")].find(node=>node.textContent.includes("dsh")).href');
    assert.equal(new URL(agentHref).searchParams.get('revision'),'browser-one');
    current='browser-two';
    await send('Page.navigate',{url:agentHref},session);
    await until('document.querySelector("#agent-title")?.textContent === "dsh" && document.querySelector("#dashboard-updated").textContent.includes("browser-one")');
    const expectedAgent=fixture.cases.find(item=>item.revision==='browser-one'&&item.path==='/api/v1/dashboard'&&item.parameters.agent==='dsh'&&item.parameters.revision==='browser-one').body;
    assert.equal((await metrics())[0],expectedAgent.totals.packages);
    assert.equal(new URL(await evaluate('document.querySelector("#dashboard-back").href')).searchParams.get('revision'),'browser-one');
    await evaluate('document.querySelector("#dashboard-back").click()');
    await until('document.querySelector("#agent-cards") && document.querySelector("#dashboard-updated").textContent.includes("browser-one")');
    assert.equal((await metrics())[1],fixture.expected['browser-one'].packages);
    checks.push('Agent and back links stay pinned after current publication changes');
    failDashboardOnce=true;
    await evaluate('document.querySelector("#dashboard-refresh").click()');
    await until('!document.querySelector("#dashboard-error").hidden');
    assert.equal((await metrics())[1],fixture.expected['browser-one'].packages);
    await evaluate('document.querySelector("#dashboard-retry").click()');
    await until('document.querySelector("#dashboard-error").hidden && document.querySelector("#dashboard-updated").textContent.includes("browser-two")');
    assert.equal((await metrics())[1],fixture.expected['browser-two'].packages);
    assert.equal(new URL(await evaluate('location.href')).searchParams.get('revision'),'browser-two');
    checks.push('refresh failure preserves prior statistics and retry commits the new revision');
    await screenshot('dashboard-desktop.png');
    await send('Page.navigate',{url:dashboardUrl+'?revision=browser-one'},session);
    await until('document.querySelector("#dashboard-updated")?.textContent.includes("browser-one")');
    await evaluate('document.querySelector("#dashboard-catalog").click()');
    await until('document.querySelectorAll(".result-card").length === 20 && document.querySelector("#revision-stamp")?.textContent.includes("browser-one")');
    assert.ok(requests.some(item=>item.route==='/api/v1/manifest'&&item.parameters.revision==='browser-one'));
    assert.ok(requests.some(item=>item.route==='/api/v1/packages'&&item.parameters.revision==='browser-one'));
    await evaluate('document.querySelector("#next-page").click()');
    await until('document.querySelectorAll(".result-card").length === 3 && document.querySelector("#page-number").textContent.includes("2")');
    checks.push('catalog link pins manifest, filters and cursor pagination to the dashboard revision');
    await send('Page.navigate',{url:dashboardUrl+'?revision=browser-one'},session);
    await until('document.querySelector("#dashboard-updated")?.textContent.includes("browser-one")');
    const detailHref=await evaluate('[...document.querySelectorAll("#recent-packages .result-link")].find(node=>node.textContent.includes("数据库工具")).href');
    await send('Page.navigate',{url:detailHref},session);
    await until('document.querySelector("h1")?.textContent.includes("数据库工具") && document.querySelector("#canonical-version")');
    assert.equal(await evaluate('window.__injected'),undefined);
    assert.ok((await evaluate('document.querySelector("main").textContent')).includes('browser-one'));
    assert.ok(requests.some(item=>item.route.endsWith('/detail')&&item.parameters.revision==='browser-one'));
    checks.push('recent metadata links open exact-revision Unicode and suffix-like package IDs');
    await send('Page.navigate',{url:dashboardUrl+'agent/?id=missing&revision=browser-one'},session);
    await until('Boolean(document.querySelector("#dashboard-error")) && !document.querySelector("#dashboard-error").hidden');
    assert.ok((await evaluate('document.querySelector("#dashboard-error-message").textContent')).includes('没有此 Agent'));
    assert.equal((await metrics()).length,0);
    const priorRequests=requests.length;
    await send('Page.navigate',{url:dashboardUrl+'?revision=browser-one&revision=browser-two'},session);
    await until('Boolean(document.querySelector("#dashboard-error")) && !document.querySelector("#dashboard-error").hidden');
    assert.equal(requests.length,priorRequests);
    checks.push('missing Agent is an explicit error and duplicate link parameters make no API request');
    retired=true;
    await send('Page.navigate',{url:dashboardUrl+'?revision=browser-one'},session);
    await until('Boolean(document.querySelector("#dashboard-error")) && !document.querySelector("#dashboard-error").hidden');
    assert.ok((await evaluate('document.querySelector("#dashboard-error-message").textContent')).includes('回收'));
    await evaluate('document.querySelector("#dashboard-refresh").click()');
    await until('document.querySelector("#dashboard-error").hidden && document.querySelector("#dashboard-updated").textContent.includes("browser-two")');
    await evaluate('window.ForgeUI.setLocale("en")');
    assert.ok((await evaluate('document.querySelector("h1").textContent')).includes('Public catalog'));
    assert.equal((await metrics())[1],fixture.expected['browser-two'].packages);
    checks.push('retired snapshot recovery and language switching retain honest statistics');
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
    await evaluate('window.ForgeUI.setTheme("dark")');
    assert.ok(await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'));
    await screenshot('dashboard-mobile-dark.png');
    checks.push('mobile layout and dark theme');
    assert.equal(network.filter(url=>new URL(url).pathname.includes('/data/')).length,0);
    assert.ok(requests.every(item=>!item.cookie&&!item.authorization));
    assert.equal(unmatched.length,0,JSON.stringify(unmatched));
    assert.equal(exceptions.length,0,JSON.stringify(exceptions));
    checks.push('no full-catalog JSON, API credentials, runtime exceptions or unmatched fixtures');
  }catch(error){
    failure=error;
    if(send&&session)try{diagnostics=await send('Runtime.evaluate',{expression:'JSON.stringify({ready:document.readyState,page:document.body.dataset.page,html:document.querySelector("main")?.innerHTML,ui:typeof window.ForgeUI,client:typeof window.ForgeQuery,api:typeof window.ForgeApiCatalog,scripts:[...document.scripts].map(x=>x.src)})',returnByValue:true},session);}catch(_){}
    throw error;
  }
  finally{
    let artifactError;
    try { fs.writeFileSync(path.join(artifacts,'dashboard-browser-results.json'),JSON.stringify({kind:fixture.kind,browser:process.env.FORGE_UI_BROWSER,checks,requests,network,exceptions,unmatched,diagnostics,error:failure&&failure.message},null,2)); } catch(error) { artifactError=error; }
    if(send)try{await send('Browser.close');}catch(_){}
    if(socket)socket.close();
    browser.kill();
    await new Promise((resolve)=>server.close(resolve));
    const resolved=path.resolve(profile),tempRoot=path.resolve(os.tmpdir());
    assert.ok(resolved.startsWith(tempRoot+path.sep)&&path.basename(resolved).startsWith('forge-query-browser-'));
    for(let attempt=0;attempt<6;attempt++){try{fs.rmSync(resolved,{recursive:true,force:true});break;}catch(error){if(attempt===5){if(!failure)throw error;}else await timers.setTimeout(100*(attempt+1));}}
    if(artifactError)throw artifactError;
  }
});

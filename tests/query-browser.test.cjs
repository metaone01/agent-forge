/* Live Edge/Chromium UI acceptance with existing tools and SQLite-generated DTO fixtures.
 * FORGE_UI_BROWSER=<installed executable> FORGE_QUERY_FIXTURE=<json> FORGE_QUERY_ARTIFACTS=<directory>
 * node --test tests/query-browser.test.cjs
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
const enabled = process.env.FORGE_UI_BROWSER && process.env.FORGE_QUERY_FIXTURE && process.env.FORGE_QUERY_ARTIFACTS;

test('API catalog in a real browser: routing, revision pagination, retries, consent and mobile layout', { skip: !enabled, timeout: 90000 }, async () => {
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_QUERY_FIXTURE,'utf8'));
  const artifacts=path.resolve(process.env.FORGE_QUERY_ARTIFACTS);
  const site=process.env.FORGE_QUERY_SITE ? path.resolve(process.env.FORGE_QUERY_SITE) : path.resolve(__dirname,'../site');
  const key=(revision,route,parameters)=>JSON.stringify([revision,route,Object.entries(parameters).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map((item)=>[key(item.revision,item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],checks=[];
  let current='browser-one',retired=false,retryFailures=0;
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length));
      const parameters=Object.fromEntries(url.searchParams);
      const revision=parameters.revision||current;
      let record=cases.get(key(revision,route,parameters));
      if(retired&&parameters.revision==='browser-one')record=cases.get(key(current,'/errors/expired',{}));
      if(parameters.q==='retryterm'&&retryFailures++===0)record=cases.get(key(current,'/errors/busy',{}));
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
    await send('Fetch.enable',{patterns:[{urlPattern:'https://media.example.test/*'}]},session);
    const evaluate=async(expression)=>{const result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;};
    const until=async(expression)=>{const start=Date.now();while(Date.now()-start<7000){if(await evaluate(expression))return;await timers.setTimeout(50);}throw new Error('Timed out: '+expression);};
    const screenshot=async(name)=>{const result=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false},session);fs.writeFileSync(path.join(artifacts,name),Buffer.from(result.data,'base64'));};
    await send('Emulation.setDeviceMetricsOverride',{width:1365,height:900,deviceScaleFactor:1,mobile:false},session);
    const detailUrl=base+'catalog/#/id/'+encodeURIComponent(fixture.id)+'?revision=browser-one&version=1.0.0';
    await send('Page.navigate',{url:detailUrl},session);
    await until('document.querySelector("h1")?.textContent.includes("数据库工具") && document.querySelector("#canonical-version")');
    assert.ok((await evaluate('document.querySelector("main").textContent')).includes('1.0.0'));
    assert.equal(await evaluate('window.__injected'),undefined);
    assert.ok((await evaluate('document.querySelector("main").textContent')).includes('指标按规范最新版本'));
    assert.ok((await evaluate('document.querySelector(".api-signals span").textContent')).includes('0'));
    assert.equal(network.filter((url)=>url.startsWith('https://media.example.test/')).length,0);
    assert.ok(requests.some((item)=>item.route.endsWith('/detail')&&item.parameters.version==='1.0.0'));
    checks.push('initial deep link, Unicode/suffix ID, exact old version and escaped metadata');
    await until('document.querySelector("#canonical-version").options.length >= 2');
    await evaluate('document.querySelector("#canonical-version").value="2.0.0";document.querySelector("#canonical-version").dispatchEvent(new Event("change",{bubbles:true}))');
    await until('location.hash.includes("version=2.0.0") && document.querySelector("#canonical-version")?.value === "2.0.0"');
    await evaluate('document.querySelector("[data-media-consent]").click()');
    await until('document.querySelector(".media-slot img")?.getAttribute("src")?.startsWith("https://media.example.test/")');
    for(let attempt=0;attempt<100&&!network.some((url)=>url.startsWith('https://media.example.test/'));attempt++)await timers.setTimeout(25);
    assert.ok(network.some((url)=>url.startsWith('https://media.example.test/')));
    checks.push('version selection and opt-in media');
    await evaluate('location.hash="#"');
    await until('document.querySelectorAll(".result-card").length === 20');
    assert.equal(await evaluate('document.querySelector("#result-count").textContent'),'20');
    const idsOne=await evaluate('[...document.querySelectorAll(".result-link")].map(node=>node.getAttribute("href"))');
    assert.ok(await evaluate('document.cookie.includes("fixture_cookie")'));
    assert.ok(requests.every((item)=>!item.cookie&&!item.authorization));
    await evaluate('document.querySelector("#next-page").click()');
    await until('document.querySelector("#page-number").textContent.includes("2") && document.querySelectorAll(".result-card").length === 3');
    const idsTwo=await evaluate('[...document.querySelectorAll(".result-link")].map(node=>node.getAttribute("href"))');
    assert.equal(new Set([...idsOne,...idsTwo]).size,23);
    assert.ok(requests.some((item)=>item.parameters.cursor&&item.parameters.revision==='browser-one'));
    await evaluate('document.querySelector("#previous-page").click()');await until('document.querySelectorAll(".result-card").length === 20');
    checks.push('global pagination, non-overlapping pages, pinned revision and omitted credentials');
    await screenshot('desktop.png');
    await evaluate('document.querySelector("#query").value="slow";document.querySelector("#query").dispatchEvent(new Event("input",{bubbles:true}))');
    for(let i=0;i<60&&!requests.some((item)=>item.parameters.q==='slow');i++)await timers.setTimeout(25);
    assert.ok(requests.some((item)=>item.parameters.q==='slow'));
    await evaluate('document.querySelector("#query").value="terminal";document.querySelector("#query").dispatchEvent(new Event("input",{bubbles:true}))');
    await until('new URL(location.href).searchParams.get("q") === "terminal" && document.querySelector("#results").getAttribute("aria-busy") === "false"');
    await timers.setTimeout(1500);
    assert.equal(await evaluate('new URL(location.href).searchParams.get("q")'),'terminal');
    assert.equal(await evaluate('document.querySelectorAll(".result-card").length'),20);
    checks.push('debounced input, cancellation and late response protection');
    await evaluate('document.querySelector("#query").value="retryterm";document.querySelector("#query").dispatchEvent(new Event("input",{bubbles:true}))');
    await until('!document.querySelector("#query-error").hidden');
    assert.equal(await evaluate('document.querySelectorAll(".result-card").length'),20);
    await evaluate('document.querySelector("#retry-query").click()');
    await until('document.querySelector("#query-error").hidden && !document.querySelector("#empty-state").hidden');
    assert.equal(requests.filter((item)=>item.parameters.q==='retryterm').length,2);
    assert.equal(await evaluate('new URL(location.href).searchParams.get("q")'),'retryterm');
    checks.push('failed query retains results and same-query retry works');
    await evaluate('document.querySelector("#query").value="";document.querySelector("#refresh-query").click()');await until('document.querySelectorAll(".result-card").length === 20 && document.querySelector("#results").getAttribute("aria-busy") === "false"');
    await evaluate('document.querySelector("#sort-select").value="downloads";document.querySelector("#sort-select").dispatchEvent(new Event("change",{bubbles:true}))');
    await until('new URL(location.href).searchParams.get("sort") === "downloads"');
    assert.ok(requests.some((item)=>item.parameters.sort==='downloads'&&item.parameters.metricSource==='npmjs.org'));
    await evaluate('window.ForgeUI.setLocale("en")');
    await until('document.querySelector("#sort-select").selectedOptions[0].textContent === "Most npm downloads"');
    assert.equal(await evaluate('document.querySelectorAll(".result-card").length'),20);
    assert.equal(await evaluate('document.querySelector("#agent-filter").options[0].textContent'),'All Agents');
    assert.ok((await evaluate('document.querySelector("#facet-filter optgroup").label')).startsWith('Declared facets'));
    checks.push('explicit download source and language switch preserve results');
    await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true},session);
    await evaluate('window.ForgeUI.setTheme("dark")');
    assert.ok(await evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1'));
    await screenshot('mobile-dark.png');
    checks.push('mobile width and dark theme');
    retired=true;current='browser-two';
    await evaluate('document.querySelector("#next-page").click()');await until('!document.querySelector("#query-error").hidden');
    assert.ok((await evaluate('document.querySelector("#query-error-message").textContent')).includes('retired'));
    await evaluate('document.querySelector("#refresh-query").click()');await until('document.querySelector("#revision-stamp").textContent.includes("browser-two") && document.querySelector("#query-error").hidden');
    checks.push('retired cursor is explicit and refresh selects current revision');
    assert.equal(network.filter((url)=>new URL(url).pathname.includes('/data/')).length,0);
    assert.equal(unmatched.length,0,JSON.stringify(unmatched));
    assert.equal(exceptions.length,0,JSON.stringify(exceptions));
    checks.push('no full catalog JSON, no runtime exceptions and no unmatched API fixtures');
  }catch(error){
    failure=error;
    if(send&&session)try{diagnostics=await send('Runtime.evaluate',{expression:'JSON.stringify({ready:document.readyState,page:document.body.dataset.page,html:document.querySelector("main")?.innerHTML,ui:typeof window.ForgeUI,client:typeof window.ForgeQuery,api:typeof window.ForgeApiCatalog,scripts:[...document.scripts].map(x=>x.src)})',returnByValue:true},session);}catch(_){}
    throw error;
  }
  finally{
    fs.writeFileSync(path.join(artifacts,'browser-results.json'),JSON.stringify({kind:fixture.kind,browser:process.env.FORGE_UI_BROWSER,checks,requests,network,exceptions,unmatched,diagnostics,error:failure&&failure.message},null,2));
    if(send)try{await send('Browser.close');}catch(_){}
    if(socket)socket.close();
    browser.kill();
    await new Promise((resolve)=>server.close(resolve));
    const resolved=path.resolve(profile),tempRoot=path.resolve(os.tmpdir());
    assert.ok(resolved.startsWith(tempRoot+path.sep)&&path.basename(resolved).startsWith('forge-query-browser-'));
    for(let attempt=0;attempt<6;attempt++){try{fs.rmSync(resolved,{recursive:true,force:true});break;}catch(error){if(attempt===5){if(!failure)throw error;}else await timers.setTimeout(100*(attempt+1));}}
  }
});

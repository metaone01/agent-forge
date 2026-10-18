/* Uses the existing Edge installation and Node CDP; all remote previews are intercepted. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const http=require('node:http');
const {spawn}=require('node:child_process');
const timers=require('node:timers/promises');
const enabled=process.env.FORGE_UI_BROWSER&&process.env.FORGE_THEME_SITE&&process.env.FORGE_THEME_FIXTURE&&process.env.FORGE_THEME_ARTIFACTS;

test('built cream theme: local assets, opt-in covers, failed previews and responsive query/docs/submission pages',{skip:!enabled,timeout:120000},async()=>{
  const site=path.resolve(process.env.FORGE_THEME_SITE),artifacts=path.resolve(process.env.FORGE_THEME_ARTIFACTS);
  fs.mkdirSync(artifacts,{recursive:true});
  const fixture=JSON.parse(fs.readFileSync(process.env.FORGE_THEME_FIXTURE,'utf8'));
  const key=(route,params)=>JSON.stringify([route,Object.entries(params).sort(([a],[b])=>a<b?-1:a>b?1:0)]);
  const cases=new Map(fixture.cases.map(item=>[key(item.path,item.parameters),item]));
  const requests=[],network=[],exceptions=[],unmatched=[],assetErrors=[],checks=[];
  const server=http.createServer((req,res)=>{
    const url=new URL(req.url,'http://localhost');
    if(url.pathname.startsWith('/project/api/v1/')){
      const route=decodeURIComponent(url.pathname.slice('/project'.length)),params=Object.fromEntries(url.searchParams);
      requests.push({route,params,cookie:req.headers.cookie||null,authorization:req.headers.authorization||null});
      const value=cases.get(key(route,params));
      if(!value){unmatched.push({route,params});res.writeHead(500,{'Content-Type':'application/json'}).end(JSON.stringify({error:{code:'fixture_missing'}}));return;}
      res.writeHead(value.status,{'Content-Type':'application/json'}).end(JSON.stringify(value.body));return;
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
    const contrast=async(selector)=>evaluate(`(()=>{const a=getComputedStyle(document.querySelector(${JSON.stringify(selector)})).color,b=getComputedStyle(document.querySelector('.result-card')).backgroundColor;const lum=c=>{const x=c.match(/[\\d.]+/g).slice(0,3).map(n=>Number(n)/255).map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4);return x[0]*.2126+x[1]*.7152+x[2]*.0722;};const x=lum(a),y=lum(b);return(Math.max(x,y)+.05)/(Math.min(x,y)+.05);})()`);
    await width(1365);await send('Page.navigate',{url:base+'catalog/'},session);
    await until("document.querySelectorAll('.result-card').length===3 && document.querySelector('#results').getAttribute('aria-busy')==='false'");
    await evaluate('ForgeUI.setTheme("light")');
    assert.match(await background(),/orchard_bg\.png\?v=[0-9a-f]{16}/);
    assert.equal(network.filter(url=>url.startsWith('https://media.example.test/')).length,0);
    assert.equal(await evaluate("document.querySelectorAll('.media-thumbnail').length"),2);
    assert.equal(await evaluate("document.querySelectorAll('.media-slot img').length"),0);
    assert.match(await evaluate("getComputedStyle(document.querySelector('.media-thumbnail')).backgroundImage"),/mc2_crate\.png/);
    assert.ok(await contrast('.result-summary')>=4.5);
    await fits();await screenshot('catalog-desktop-light.png');
    checks.push('cream palette/local versioned background and crate; all plugin types use first preview without remote requests');
    await evaluate("document.querySelector('[data-media-consent]').click()");
    await until("document.querySelector('.media-thumbnail img')?.complete && document.querySelector('.media-thumbnail img').naturalWidth===1");
    await until("Array.from(document.querySelectorAll('.media-thumbnail img')).some(image=>image.hidden)");
    assert.equal(await evaluate("document.querySelector('.media-thumbnail img').getAttribute('referrerpolicy')"),'no-referrer');
    assert.equal(await evaluate("document.querySelector('.media-thumbnail img').loading"),'lazy');
    const heights=await evaluate("Array.from(document.querySelectorAll('.media-thumbnail')).map(node=>node.getBoundingClientRect().height)");
    assert.ok(Math.abs(heights[0]-heights[1])<1);
    assert.ok((await evaluate("document.querySelector('#results').textContent")).includes('加载失败'));
    await evaluate("document.querySelector('[data-media-consent]').click()");
    assert.equal(await evaluate("document.querySelectorAll('.media-slot img').length"),0);
    checks.push('opt-in images, no-referrer/lazy, failed image restores fixed crate slot and consent can be revoked');
    await evaluate('ForgeUI.setTheme("dark");ForgeUI.setLocale("en")');
    assert.match(await background(),/orchard_night\.png/);assert.ok(await contrast('.result-summary')>=4.5);
    await width(390);await fits();await screenshot('catalog-mobile-dark.png');
    await width(320);await fits();
    checks.push('night palette, English, 390px/320px catalog and measured secondary-text contrast');
    await evaluate("document.querySelector('.result-link').click()");
    await until("document.querySelector('#canonical-version') && document.querySelector('.detail-heading')");
    await fits();await screenshot('detail-mobile-dark.png');
    assert.ok((await evaluate("document.querySelector('main').textContent")).includes('1.0.0'));
    checks.push('exact-version detail keeps shared dark surfaces and fits 320px');
    await width(1365);await send('Page.navigate',{url:base+'catalog/dashboard/'},session);
    await until("document.querySelector('#dashboard-updated')?.textContent.includes('theme-one')");
    assert.match(await background(),/orchard_night\.png/);await fits();await screenshot('dashboard-desktop-dark.png');
    await width(320);await fits();await screenshot('dashboard-mobile-dark.png');
    checks.push('public SQLite dashboard uses same theme on desktop and 320px');
    await width(1365);await send('Page.navigate',{url:base+'docs/index_en.html'},session);
    await until("document.querySelector('.doc-content h1') && typeof ForgeUI!=='undefined'");
    await evaluate('ForgeUI.setTheme("light")');await fits();await screenshot('docs-desktop-light.png');
    await width(320);await fits();await screenshot('docs-mobile-light.png');
    checks.push('generated English docs, sidebar/code surfaces and 320px layout');
    await width(1365);await send('Page.navigate',{url:base+'submit/'},session);
    await until("document.querySelector('.schema-field input')");
    await fits();await screenshot('submit-desktop-light.png');
    await evaluate('ForgeUI.setTheme("dark")');await width(320);await fits();await screenshot('submit-mobile-dark.png');
    checks.push('submission schema form/JSON preview keep shared light and night surfaces at both widths');
    assert.deepEqual(assetErrors,[]);assert.deepEqual(unmatched,[]);assert.deepEqual(exceptions,[]);
    assert.ok(requests.every(request=>!request.cookie&&!request.authorization));
    assert.ok(!network.some(url=>/\/data\/.*index\.json/.test(url)));
    const ownBadges=['validate','project','release'].map(name=>'https://github.com/metaone01/agent-forge/actions/workflows/'+name+'.yml/badge.svg');
    const external=network.filter(url=>!ownBadges.includes(url)&&!url.startsWith(base)&&!url.startsWith('https://media.example.test/')&&!url.includes('me.kis.v2.scr.kaspersky-labs.com')&&!url.startsWith('http://127.0.0.1:'));
    assert.deepEqual(external,[]);
    checks.push('no asset failures, unmatched APIs, runtime exceptions, credentials, full indexes or unexpected external requests; own documentation badges are intercepted');
  }catch(error){failure=error;if(send&&session){try{const value=await send('Page.captureScreenshot',{format:'png'},session);fs.writeFileSync(path.join(artifacts,'failure.png'),Buffer.from(value.data,'base64'));}catch(_){}}}
  finally{
    fs.writeFileSync(path.join(artifacts,'theme-browser-results.json'),JSON.stringify({kind:fixture.kind,checks,requests,network,exceptions,unmatched,assetErrors,error:failure?failure.message:null,browser:process.env.FORGE_UI_BROWSER},null,2));
    if(socket)socket.close();browser.kill();await new Promise(resolve=>server.close(resolve));
  }
  if(failure)throw failure;
});

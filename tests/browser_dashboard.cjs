/* Optional developer-only browser check. Uses an already-installed Playwright. */
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES
  ? path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES, 'playwright') : 'playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:1600,height:1200}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  try {
    await page.goto(process.env.DASHBOARD_TEST_URL || 'http://127.0.0.1:8765');
    await page.waitForFunction(()=>document.querySelectorAll('.run-button').length===12);
    assert.equal(await page.locator('.stage-group').count(),7);
    assert.equal(await page.locator('button').count(),12); // Only run-selection controls.
    const cases=[['#1','done'],['#2','done'],['#3','approval-required'],['#4','approval-required'],
      ['#5','approval-required'],['#6','approval-required'],['#7','approval-required'],
      ['#8','approval-required'],['#9','unknown'],['#10','unknown'],['#11','pr-ready'],['#12','done']];
    for(const [issue,status] of cases){
      await page.locator('.run-button').filter({has:page.locator('strong',{hasText:new RegExp('^Issue '+issue+'$')})}).click();
      assert.match(await page.locator('#run-summary').innerText(),new RegExp(status));
    }
    await page.locator('.run-button').filter({has:page.locator('strong',{hasText:/^Issue #11$/})}).click();
    assert.match(await page.locator('#pipeline').innerText(),/skipped/);
    await page.locator('.run-button').filter({has:page.locator('strong',{hasText:/^Issue #2$/})}).click();
    assert.match(await page.locator('#tasks').innerText(),/Repairs: 1/);
    const out=process.env.DASHBOARD_TEST_ARTIFACTS;
    if(out){fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,'dashboard-desktop.png'),fullPage:true});}
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
    if(out)await page.screenshot({path:path.join(out,'dashboard-mobile.png'),fullPage:true});
    await page.route('**/api/snapshot*',async route=>{
      const response=await route.fetch();const data=await response.json();data.changed=true;
      const selected=data.runs.find(r=>r.issue===2);
      selected.diagnostics.push({outcome:'ERROR',diagnostic_code:'TEST',summary:'<img src=x onerror="window.xss=1">',stage:'task_gate',task_id:'task-1',attempt:1,timestamp:new Date().toISOString()});
      await route.fulfill({response,json:data});
    });
    await page.waitForFunction(()=>document.getElementById('diagnostics').textContent.includes('<img src=x'),{timeout:15000});
    assert.equal(await page.locator('#diagnostics img').count(),0);
    assert.equal(await page.evaluate(()=>window.xss),undefined);
    await page.unroute('**/api/snapshot*');
    await page.route('**/api/snapshot*',route=>route.abort());
    await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('API disconnected'),{timeout:15000});
    assert.match(await page.locator('#connection').innerText(),/stale/);
    assert.match(await page.locator('#run-summary').innerText(),/Issue #2/);
    assert.deepEqual(errors,[]);
    console.log('Browser checks passed: 12 scenarios, responsive layout, XSS text rendering, offline freshness.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});

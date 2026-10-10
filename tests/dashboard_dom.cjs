/* Dependency-free DOM boundary test; not a substitute for a real browser. */
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),assert=require('node:assert/strict');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.attrs={};this.style={};this.events={};this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(_){throw Error('Unsafe HTML rendering');}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this._text='';this.children=children;}
  setAttribute(k,v){this.attrs[k]=v;}
  addEventListener(k,v){this.events[k]=v;}
  querySelector(selector){if(selector==='dd')return this.children.find(c=>c.tag==='dd');const match=selector.match(/data-run-id="([^"]+)"/);return match?this.children.find(c=>c.dataset.runId===match[1]):undefined;}
  focus(){document.activeElement=this;}
}
const ids=['connection','last-poll','queue-freshness','worker-health','worker-detail','queue-count','run-count','run-list-count','quality','quality-detail','run-list','queue-note','queue-list','legacy-panel','legacy-list','run-summary','pipeline','tasks','delivery','diagnostics','task-progress','diagnostic-count'];
const elements=Object.fromEntries(ids.map(id=>[id,new Element('div')]));
const document={activeElement:null,createElement:tag=>new Element(tag),getElementById:id=>elements[id]||findId(id)};
function findId(id){const scan=e=>e.id===id?e:e.children.map(scan).find(Boolean);return Object.values(elements).map(scan).find(Boolean);}
let payload=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),fail=false;
const context=vm.createContext({document,URL,AbortController,Date,CSS:{escape:s=>s},console,
  setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,
  fetch:async()=>{if(fail)throw Error('offline');return {ok:true,json:async()=>JSON.parse(JSON.stringify(payload))};}});
const source=fs.readFileSync(path.join(__dirname,'../dashboard_static/app.js'),'utf8');
assert.doesNotMatch(source,/\binnerHTML\b|\bouterHTML\b|\binsertAdjacentHTML\b|\beval\s*\(/);
vm.runInContext(source,context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(elements['run-list'].children.length,12);
  assert.equal(elements.pipeline.children.length,7);
  const select=issue=>{const button=elements['run-list'].children.find(b=>b.children[0].textContent==='Issue #'+issue);assert.ok(button);button.events.click();};
  select(2);assert.match(elements.tasks.textContent,/Repairs: 1/);
  select(4);assert.match(elements.diagnostics.textContent,/VERIFIER_RISK/);
  select(5);assert.match(elements.diagnostics.textContent,/VERIFIER_ERROR/);
  select(9);assert.match(elements['run-summary'].textContent,/no observed end/);
  select(10);assert.match(elements['run-summary'].textContent,/History incomplete/);
  select(11);assert.match(elements.pipeline.textContent,/skipped/);
  assert.match(elements.delivery.textContent,/Not confirmed/);
  select(12);assert.match(elements.pipeline.textContent,/DEBATE critique✓ PASS/);
  payload.runs.find(r=>r.issue===12).diagnostics.push({outcome:'ERROR',diagnostic_code:'TEST',summary:'<img src=x onerror="pwned()">',stage:'task_gate',attempt:1,timestamp:payload.generated_at});payload.changed=true;
  await vm.runInContext('poll()',context);
  assert.match(elements.diagnostics.textContent,/<img src=x onerror/);
  assert.equal(elements.diagnostics.children.some(c=>c.tag==='img'),false);
  fail=true;await vm.runInContext('poll()',context);
  assert.match(elements.connection.textContent,/API disconnected.*stale/);
  assert.match(elements['run-summary'].textContent,/Issue #12/);
  fail=false;payload.runs=[];payload.changed=true;
  await vm.runInContext('poll()',context);
  assert.doesNotMatch(elements['run-summary'].textContent,/Issue #12/);
  assert.match(elements['run-summary'].textContent,/No event history/);
  assert.equal(elements['task-progress'].textContent,'—');
  assert.equal(elements['diagnostic-count'].textContent,'0');
  console.log('DOM boundary checks passed: scenario rendering, selection, ERROR/RISK, skipped, unknown, XSS-as-text and offline warning. Real browser layout remains unverified.');
})().catch(e=>{console.error(e);process.exit(1);});

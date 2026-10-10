/* Read-only rendering: all event-derived strings become text nodes. */
'use strict';
const groups = [
  ['Source', [['claim', 'Issue claim']]],
  ['Environment', [['worktree', 'Worktree'], ['bootstrap', 'Bootstrap'], ['context', 'Context']]],
  ['Plan', [['debate', 'DEBATE critique'], ['plan', 'Plan generation'], ['plan_gate', 'Plan Gate']]],
  ['Tasks ↻', [['task', 'Implementation'], ['task_validation', 'Validation'], ['task_gate', 'Task Gate'], ['repair', 'Repair loop'], ['task_commit', 'Task commit']]],
  ['Validate', [['full_validation', 'Full validation']]],
  ['Deliver', [['pr_prepare', 'PR preparation'], ['pr_publish', 'PR publication']]],
  ['Finish', [['ci', 'Required CI'], ['merge_gate', 'Merge Gate'], ['merge', 'Merge confirmed'], ['issue_close', 'Issue close']]],
];
const icons = {PASS:'✓',done:'✓',FAIL:'!',ERROR:'!',RISK:'!',failed:'!',started:'…',pending:'…',running:'…',skipped:'↷',unknown:'?',stale:'?',exited:'○','approval-required':'!','pr-ready':'◈'};
let snapshot = null, selected = null, lastPoll = null, disconnected = false, pollTimer = null;
const byId = id => document.getElementById(id);
function node(tag, text, className) {const e=document.createElement(tag); if(text!==undefined)e.textContent=String(text); if(className)e.className=className; return e;}
function badge(value) {const status=Object.hasOwn(icons,value)?value:'unknown'; return node('span', icons[status]+' '+status, 'badge '+status);}
function replace(id, ...children) {byId(id).replaceChildren(...children);}
function ageSeconds(timestamp) {if(!timestamp)return null; const t=Date.parse(timestamp); return Number.isFinite(t)?Math.max(0,Math.floor((Date.now()-t)/1000)):null;}
function duration(n) {if(n===null||!Number.isFinite(n))return 'unknown'; if(n<60)return n+'s'; if(n<3600)return Math.floor(n/60)+'m '+n%60+'s'; return Math.floor(n/3600)+'h '+Math.floor(n%3600/60)+'m';}
function timeLabel(value) {return value?new Date(value).toLocaleString():'unknown';}
function link(text, url) {const e=node('a',text); const u=new URL(url); if(u.protocol==='https:'&&u.hostname==='github.com'&&!u.username&&!u.password){e.href=u.href;e.target='_blank';e.rel='noopener noreferrer';}return e;}
function empty(message) {return node('p',message,'empty');}
function metadata(entries, className='run-meta') {const dl=node('dl',undefined,className); for(const [label,value] of entries){const wrapper=node('div');wrapper.append(node('dt',label),node('dd',value===null||value===undefined?'unknown':value));dl.append(wrapper);}return dl;}
function updateClock() {
  byId('last-poll').textContent=lastPoll?'API observed '+duration(Math.floor((Date.now()-lastPoll)/1000))+' ago':'No API observation yet';
  if(!snapshot)return;
  const q=snapshot.queue, w=snapshot.worker;
  const qa=ageSeconds(q.timestamp), wa=ageSeconds(w.timestamp);
  byId('queue-freshness').textContent=qa===null?'No recorded snapshot':(qa>60||q.freshness!=='observed'?'Stale / unknown · ':'Observed · ')+duration(qa)+' ago';
  byId('worker-health').textContent=w.status==='exited'?'Exited':wa!==null&&wa>60?'Stale / unknown':'Unknown';
  byId('worker-detail').textContent=(w.last_result?'Last observation: '+w.last_result+' · ':'')+'no live heartbeat';
  const run=snapshot.runs.find(r=>r.id===selected);
  if(run&&byId('run-age'))byId('run-age').textContent=duration(ageSeconds(run.updated_at))+' ago';
  if(run&&byId('run-elapsed')){const seconds=run.ended_at&&run.started_at?Math.max(0,Math.floor((Date.parse(run.ended_at)-Date.parse(run.started_at))/1000)):ageSeconds(run.started_at);byId('run-elapsed').textContent=duration(seconds);}
}
function renderOverview() {
  byId('queue-count').textContent=snapshot.queue.observed_count===null?'Unknown':String(snapshot.queue.observed_count)+' observed';
  byId('run-count').textContent=String(snapshot.runs.length);
  byId('run-list-count').textContent=String(snapshot.runs.length);
  byId('quality').textContent=snapshot.quality==='observed'?'Observed':'Unknown / incomplete';
  byId('quality-detail').textContent=snapshot.problems.length?snapshot.problems.join(', '):'No detected history gaps';
  const focused=document.activeElement?.dataset?.runId;
  replace('run-list', ...snapshot.runs.map(run=>{
    const button=node('button',undefined,'run-button'+(run.id===selected?' selected':''));button.type='button';button.dataset.runId=run.id;button.setAttribute('aria-pressed',String(run.id===selected));
    button.append(node('strong',run.issue?'Issue #'+run.issue:'Worker invocation'),node('small',(run.repository||'Unknown repository')+' · '+run.id.slice(0,8)),badge(run.status));
    button.addEventListener('click',()=>{selected=run.id;renderOverview();renderRun();});return button;
  }));
  if(!snapshot.runs.length)replace('run-list',empty('No recorded runs.'));
  if(focused)byId('run-list').querySelector('[data-run-id="'+CSS.escape(focused)+'"]')?.focus();
  byId('queue-note').textContent='Observed up to '+(snapshot.queue.limit||100)+' issues. Total queue size is unknown; eligibility is checked by the worker.';
  replace('queue-list',...snapshot.queue.issues.map(number=>{const item=node('div',undefined,'queue-item');item.append(snapshot.queue.repository?link('Issue #'+number,'https://github.com/'+snapshot.queue.repository+'/issues/'+number):node('span','Issue #'+number));return item;}));
  if(!snapshot.queue.issues.length)replace('queue-list',empty(snapshot.queue.timestamp?'No issues in this snapshot.':'No queue history.'));
  byId('legacy-panel').hidden=!snapshot.legacy.length;
  replace('legacy-list',...snapshot.legacy.map(item=>{const e=node('div',undefined,'legacy-item');e.append(node('span','Issue #'+item.issue+' '),badge(item.state));return e;}));
  updateClock();
}
function renderRun() {
  const run=snapshot?.runs.find(r=>r.id===selected);
  if(!run){replace('run-summary',empty('No event history available.'));byId('task-progress').textContent='—';byId('diagnostic-count').textContent='0';replace('pipeline',empty('No recorded stage evidence.'));replace('tasks',empty('No recorded tasks.'));replace('delivery',empty('No PR/CI evidence.'));replace('diagnostics',empty('No recorded diagnostics.'));return;}
  const heading=node('div',undefined,'run-summary-top');heading.append(node('h2',run.issue?'Issue #'+run.issue:'Worker invocation'),badge(run.status),node('span',run.mode||'Mode unknown','badge'));
  const repo=node('p',run.repository||'Unknown repository','repository');
  const meta=metadata([['Run',run.id.slice(0,8)],['Current / last stage',run.current_stage],['Elapsed',duration(run.elapsed_seconds)],['Last event',duration(run.age_seconds)+' ago'],['Publication',run.publish===true?'Authorized':run.publish===false?'Dry-run':'Unknown'],['Plan attempts / repairs',run.plan_attempts+' / '+run.plan_repairs]]);
  meta.children[2].querySelector('dd').id='run-elapsed';meta.children[3].querySelector('dd').id='run-age';
  const notes=[];
  if(run.quality!=='observed')notes.push(node('p','History incomplete: '+run.problems.join(', ')+'. Completion is unknown.','warning-text'));
  if(!run.ended_at)notes.push(node('p','This run has no observed end. Worker liveness is unknown; a crash or interrupted observation is possible.','warning-text'));
  replace('run-summary',heading,repo,meta,...notes);
  replace('pipeline',...groups.map(([title,stages])=>{const group=node('div',undefined,'stage-group');group.append(node('h3',title));for(const [key,label] of stages){const status=run.stages[key]?.outcome||'unknown';const card=node('div',undefined,'stage-node '+(Object.hasOwn(icons,status)?status:'unknown')+(key==='repair'?' repair-loop':''));card.append(node('strong',label),badge(status));group.append(card);}return group;}));
  const done=run.tasks.filter(t=>t.status==='done').length;
  byId('task-progress').textContent=done+' / '+run.tasks.length+' completed';
  const track=node('div',undefined,'progress-track'),fill=node('div',undefined,'progress-fill');
  // Width is a bounded numeric value, not untrusted markup.
  fill.style.width=(run.tasks.length?Math.round(done/run.tasks.length*100):0)+'%';track.append(fill);
  replace('tasks',track,...run.tasks.map(task=>{const card=node('article',undefined,'task-card');const heading=node('div',undefined,'task-heading');heading.append(node('h3',task.id),badge(task.status));card.append(heading,node('p','Attempts: '+task.attempts+' · Repairs: '+task.repairs+' · Criteria: '+(task.criteria_count??'unknown')+' (wording withheld; consult private state)','task-detail'));const stages=node('div',undefined,'task-stages');for(const key of ['task_validation','task_gate','repair','task_commit']){const value=task.stages[key];const item=node('div',key.replaceAll('_',' '),'task-stage');item.append(badge(value?.outcome||'unknown'));stages.append(item);}card.append(stages);return card;}));
  if(!run.tasks.length)replace('tasks',empty('No task history recorded.'));
  const pr=run.pr;
  const prHeading=node('p');prHeading.append(pr.published&&pr.url?link('PR #'+pr.number,pr.url):node('span',run.publish===false?'Local PR preparation · not published':'No published PR observed'));
  const deliver=metadata([['Head',pr.head],['Base',pr.base],['Merge confirmation',pr.merge_confirmed?'Confirmed by worker':'Not confirmed']], 'delivery-meta');
  replace('delivery',prHeading,deliver,...run.ci.map(check=>{const row=node('div',undefined,'check-row');row.append(node('span',check.identifier,'mono'),badge(check.outcome));return row;}));
  if(!run.ci.length)byId('delivery').append(empty('No required CI observations. Names are never inferred.'));
  byId('diagnostic-count').textContent=String(run.diagnostics.length);
  replace('diagnostics',...run.diagnostics.slice().reverse().map(d=>{const item=node('div',undefined,'diagnostic');item.append(badge(d.outcome),node('strong',d.diagnostic_code),node('p',d.summary),node('small',d.stage+(d.task_id?' · '+d.task_id:'')+' · attempt '+d.attempt+' · '+timeLabel(d.timestamp)));return item;}));
  if(!run.diagnostics.length)replace('diagnostics',empty('No safe diagnostic records. Missing evidence is not proof of success.'));
  updateClock();
}
async function poll() {
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),4000);
  try {
    const response=await fetch('/api/snapshot'+(snapshot?'?cursor='+encodeURIComponent(snapshot.cursor):''),{cache:'no-store',signal:controller.signal});
    if(!response.ok)throw new Error('Observer unavailable');
    const value=await response.json();
    if(value.version!==1||!Array.isArray(value.runs))throw new Error('Unsupported observation');
    const changed=!snapshot||value.changed; snapshot=value;lastPoll=Date.now();disconnected=false;
    if(!snapshot.runs.some(r=>r.id===selected))selected=snapshot.runs[0]?.id||null;
    const notice=byId('connection');notice.className='notice'+(value.quality==='observed'?'':' warning');notice.textContent=value.quality==='observed'?'Observer connected · recorded history only; no live heartbeat.':'Observer connected · history incomplete or unavailable. Unknown states are retained.';
    if(changed){renderOverview();renderRun();}else updateClock();
  } catch (_) {
    disconnected=true;const notice=byId('connection');notice.className='notice error';notice.textContent='API disconnected · displayed data is stale. Last successful API observation: '+(lastPoll?new Date(lastPoll).toLocaleString():'none')+'.';
  } finally {clearTimeout(timeout);pollTimer=setTimeout(poll,5000);}
}
setInterval(updateClock,1000);
poll();

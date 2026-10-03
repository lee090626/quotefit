'use strict';
const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>Number(n||0).toLocaleString('ko-KR');
const MAX_TEXT_BYTES=5*1024*1024;
let state={quotes:[],samples:{}}, current=null, sourceFile=null, missing=[], busy=false, resultDirty=false, approvalStateUnverified=false;
const labels={pending:'승인 대기',approved:'내부 승인 완료',stale:'재검증 필요',invalidated:'재검증 필요',expired:'유효기간 만료',needs_input:'사양 확인 필요',saved:'초안 저장 완료',blocked:'담당자 확인 필요'};
const actionNames={parse_rfq:'요청서 읽기',ask_missing:'누락 사양 확인 계획',lookup:'회사 기준 조회 계획',lookup_snapshot:'원가·재고·생산 여력 조회',calculate:'가격·납기 계산',compare_request:'이전 견적과 변경 비교',requery_all:'변경·만료 조건 재조회 계획',calculate_standard:'표준 조건 재계산',try_alternative:'납기 충돌 해결 계획',handoff:'담당자 확인으로 인계',calculate_alternative:'대안 계산·재검증',verify_alternative:'대안 조건 확인',save_and_readback:'견적·승인 작업 저장 확인',input_guard:'문서 입력 안전 확인',save:'저장 처리'};
async function api(path,body){
  let response;
  try{response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined});}
  catch{throw new Error('로컬 서버에 연결하지 못했습니다. 실행 상태를 확인한 뒤 새로고침하세요.');}
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||data.message||'처리하지 못했습니다. 내용을 확인한 뒤 다시 시도해 주세요.');
  return data;
}
function notify(message,type=''){const box=$('notice');box.textContent=message;box.className=`notice ${type}`;box.hidden=false;}
function updateTextLimit(){
  const tooLarge=!sourceFile&&new TextEncoder().encode($('request').value).byteLength>MAX_TEXT_BYTES;
  $('request').setAttribute('aria-invalid',String(tooLarge));
  $('request-limit').textContent=tooLarge?'요청 텍스트가 UTF-8 기준 5MiB를 넘었습니다. 줄이거나 5MiB 이하 파일을 선택하세요.':'요청 텍스트는 UTF-8 기준 5MiB 이하입니다. 파일도 5MiB 이하만 지원합니다.';
  $('run-button').disabled=busy||tooLarge||approvalStateUnverified;
  $('revalidate').disabled=busy||approvalStateUnverified;
  return tooLarge;
}
function setBusy(value){busy=value;document.querySelectorAll('button,select,input').forEach(el=>el.disabled=value);$('request').readOnly=value;if(!value){$('run-button').textContent=missing.length?'확인값으로 다시 검증 →':'검증 시작 →';document.querySelectorAll('[data-approve]').forEach(el=>el.disabled=resultDirty||approvalStateUnverified||current?.approval?.status!=='pending');}$('run-form').setAttribute('aria-busy',String(value));updateTextLimit();}
function cacheQuote(quote){
  const index=state.quotes.findIndex(item=>item.id===quote.id&&item.version===quote.version);
  if(index<0)state.quotes.push(quote);else state.quotes[index]=quote;
}
function renderStoredQuote(quote){
  const latest=state.quotes.filter(item=>item.id===quote.id).sort((a,b)=>b.version-a.version)[0];
  if(!latest)throw new Error('저장된 견적 상태를 다시 확인하지 못했습니다. 새로고침하세요.');
  renderQuote(latest);
}
async function showSavedQuote(quote){
  cacheQuote(quote);
  approvalStateUnverified=true;
  renderQuote(quote);
  try{await refresh();renderStoredQuote(quote);return true;}
  catch{approvalStateUnverified=true;renderQuote(quote);notify('견적은 저장됐지만 최신 상태를 다시 읽지 못했습니다. 재실행하지 말고 새로고침해 확인하세요.','error');return false;}
}
function renderHistory(){
  $('history-count').textContent=state.quotes.length;
  $('saved-quote').innerHTML='<option value="">견적 선택</option>'+state.quotes.slice().reverse().map(q=>`<option value="${Number(q.id)}:${Number(q.version)}" ${current?.id===q.id&&current?.version===q.version?'selected':''}>#${Number(q.id)} · V${Number(q.version)} · ${esc(labels[q.approval?.status]||q.approval?.status)}</option>`).join('');
  $('history').innerHTML=state.quotes.length?state.quotes.slice().reverse().map(q=>`<button type="button" data-quote="${Number(q.id)}" data-version="${Number(q.version)}" class="${current?.id===q.id&&current?.version===q.version?'selected':''}"><strong>견적 #${Number(q.root_id||q.id)} · V${Number(q.version)}</strong><small>${esc(labels[q.approval?.status]||q.approval?.status||'승인 대기')} · 영업일 ${Number(q.day)}</small></button>`).join(''):'<p class="sidebar-muted">완성한 초안이 여기에 저장돼요.</p>';
}
function renderLogs(logs=[]){
  $('log-count').textContent=logs.length;
  $('logs').innerHTML=logs.length?logs.map(log=>{
    const title=log.title||actionNames[log.action]||log.tool||log.action||log.stage||'작업 확인';
    const detail=log.message||log.reason||log.summary||log.detail||'';
    const failed=log.success===false||['failed','fallback','error','blocked','warning'].includes(log.status);
    const provider=log.provider==='ollama-local'?'로컬 AI':log.provider==='rules'?'규칙 경로':'검증 도구';
    const stamp=log.time||log.timestamp||log.at;
    const time=stamp?new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(stamp)):'';
    return `<li class="${failed?'failed':''}"><div class="log-title">${esc(title)}</div><div class="log-detail">${esc(detail)}</div><div class="log-meta"><span>${provider}</span><span>${esc(time)}</span></div><details><summary>입력·결과 보기</summary><pre>${esc(JSON.stringify(log,null,2))}</pre></details></li>`;
  }).join(''):'<li class="log-empty">검증을 시작하면 실제 실행 기록이 표시됩니다.</li>';
}
function renderMissing(entries=[]){
  missing=entries;
  $('confirmation').hidden=!entries.length;
  $('missing-fields').innerHTML=entries.map((entry,index)=>{
    const code=entry.item_code||entry.code||String(entry);
    return `<div class="confirm-field"><label for="confirm-${index}">${esc(code)} · ${esc(entry.message||entry.label||'재질 확인값')}<input id="confirm-${index}" data-code="${esc(code)}" type="text" placeholder="고객에게 확인한 재질 입력" autocomplete="off" required></label></div>`;
  }).join('');
  if(entries.length)$('run-button').textContent='확인값으로 다시 검증 →';
}
function describeChange(change){
  if(typeof change==='string')return change;
  return change.message||change.description||`${change.item_code||change.code||''} ${change.field||''}: ${change.before??change.old??''} → ${change.after??change.new??''}`;
}
function renderQuote(quote){
  resultDirty=false;
  current=quote;$('empty-result').hidden=true;$('result-content').hidden=false;
  $('version-label').textContent=`견적 #${quote.root_id||quote.id} · V${quote.version}`;
  const approval=quote.approval||{};
  const status=approval.status||'pending';
  const modeText=quote.planner_mode==='ai'?'로컬 AI 판단 실행':quote.planner_mode==='mixed'?'AI·규칙 혼합 판단 · 전체 AI 성공 아님':quote.planner_mode==='rules_fallback'?'AI 판단 실패 · 규칙 경로 사용':'규칙 경로 · AI 사용 안 함';
  let html=`<div class="result-summary"><span class="badge ${status==='approved'?'good':status==='pending'&&!approvalStateUnverified?'neutral':'warn'}">${esc(approvalStateUnverified?'승인 상태 확인 필요':labels[status]||status)}</span><span class="badge ${quote.planner_mode==='ai'?'good':'warn'}">${esc(modeText)}</span></div><p class="result-message">현재 조회 조건으로 계산한 초안입니다. 내부 승인 후에도 실제 발주·재고 예약은 별도 확인이 필요합니다.</p>`;
  if(quote.changes?.length)html+=`<div class="changes"><strong>이전 견적에서 달라진 조건</strong><ul>${quote.changes.map(c=>`<li>${esc(describeChange(c))}</li>`).join('')}</ul></div>`;
  for(const option of quote.options||[]){
    const approved=status==='approved'&&approval.option_id===option.id;
    const selectable=status==='pending'&&!approvalStateUnverified;
    html+=`<article class="option"><div class="option-heading"><h3>${esc(option.label)}</h3><span class="badge ${option.meets_requested?'good':'warn'}">${option.meets_requested?'요청 납기 충족':'납기 협의 필요'}</span></div><div class="option-metrics"><div class="amount">${money(option.total_krw)}<small>원 · VAT 별도</small></div><div class="delivery">${Number(option.delivery_days)}영업일</div></div><p class="option-footnote">총원가 ${money(option.cost_krw)}원 · 원가 가산율 25% · 매출 기준 이익률 20%</p><details><summary>품목별 금액·계산 근거</summary><div class="table-wrap"><table><thead><tr><th>품목</th><th class="num">수량</th><th class="num">개당 기본원가</th><th class="num">작업 추가비</th><th class="num">견적액</th></tr></thead><tbody>${(option.lines||[]).map(line=>`<tr><td>${esc(line.item_code)} ${esc(line.item_name)}</td><td class="num">${Number(line.quantity)}</td><td class="num">${money(line.base_unit_cost_krw ?? line.unit_cost_krw)}</td><td class="num">${money(line.extra_total_krw ?? 0)}</td><td class="num">${money(line.total_krw)}</td></tr>`).join('')}</tbody></table></div><pre>${esc(JSON.stringify(option.assumptions||[],null,2))}</pre></details><div class="option-actions">${!option.meets_requested&&selectable?`<label class="agreement"><input type="checkbox" data-agreement="${esc(option.id)}">고객과 ${Number(option.delivery_days)}영업일 납기로 변경 합의</label>`:'<span class="muted">재고·생산 슬롯 예약은 수행하지 않습니다.</span>'}${selectable?`<button type="button" class="approve" data-approve="${esc(option.id)}">이 조건으로 내부 승인</button>`:`<span class="badge ${approved?'good':'neutral'}">${approved?'선택한 승인 조건':status==='approved'?'다른 조건 승인됨':esc(labels[status]||status)}</span>`}</div></article>`;
  }
  html+=`<div class="export-row"><span>가상 회사 자료 ${esc(quote.snapshot_id)} · 영업일 ${Number(quote.day)} 조회</span><a href="/api/export?quote_id=${Number(quote.id)}&version=${Number(quote.version)}" download="견적핏_견적_${Number(quote.id)}_V${Number(quote.version)}.json">견적·근거 내려받기</a></div>`;
  $('revalidate').hidden=!state.quotes.some(q=>q.id===quote.id&&q.version===quote.version&&q.approval?.status!=='invalidated');
  $('result-content').innerHTML=html;renderLogs(quote.logs||[]);renderHistory();
}
async function refresh(){
  const latestState=await api('/api/state');
  approvalStateUnverified=false;
  state=latestState;
  if(state.day!==undefined)$('day').value=String(state.day);
  const health=$('ai-health');health.textContent=state.ai?.available?'로컬 AI 연결됨':'로컬 AI 연결 필요';health.className=`badge ${state.ai?.available?'good':'warn'}`;
  renderHistory();
  if(current){const versions=state.quotes.filter(q=>q.id===current.id);const latest=versions.sort((a,b)=>b.version-a.version)[0];const updated=latest||state.quotes.find(q=>q.id===current.id&&q.version===current.version);if(updated){current=updated;if(!resultDirty)renderQuote(updated);}}
}
function clearConfirm(){renderMissing([]);}
function hideResult(){resultDirty=true;$('result-content').hidden=true;$('empty-result').hidden=false;$('version-label').textContent='';$('revalidate').hidden=true;}
async function loadSample(kind){
  if(busy)return;
  if(kind==='order'&&!current){notify('먼저 최초 견적을 검증·저장한 뒤 발주 변경을 불러오세요.');return;}
  hideResult();
  const day=kind==='order'?4:0;
  setBusy(true);
  try{await api('/api/clock',{day});await refresh();$('kind').value=kind;$('request').value=state.samples[kind]||'';sourceFile=null;updateTextLimit();$('upload').value='';$('source-label').textContent='가상 시연 요청 · TXT';clearConfirm();notify(kind==='order'?'영업일 4의 발주 변경 요청을 불러왔습니다. 기존 견적과 최신 조건을 비교하세요.':'최초 요청을 불러왔습니다. 검증을 시작하면 누락 사양을 확인합니다.');}
  catch(error){notify(error.message,'error');}finally{setBusy(false);}
}
$('sample-rfq').addEventListener('click',()=>loadSample('rfq'));
$('sample-order').addEventListener('click',()=>loadSample('order'));
$('revalidate').addEventListener('click',async()=>{
  if(busy||approvalStateUnverified||!current)return;
  const latest=state.quotes.filter(q=>q.id===current.id).sort((a,b)=>b.version-a.version)[0];
  if(!latest||latest.version!==current.version){notify('견적 버전이 바뀌었습니다. 최신 상태를 불러오세요.','error');await refresh();return;}
  setBusy(true);hideResult();
  try{const result=await api('/api/run',{kind:'revalidate',quote_id:current.id,expected_version:current.version,day:Number($('day').value),mode:$('mode').value});if(result.quote){if(await showSavedQuote(result.quote))notify('저장된 요청과 확인값으로 최신 회사 기준을 다시 조회했습니다.','success');}else{await refresh();renderLogs(result.logs||[]);notify(result.message||result.title||'재검증을 완료하지 못했습니다.','error');}}
  catch(error){notify(error.message,'error');try{await refresh();}catch{}}
  finally{setBusy(false);}
});
$('request').addEventListener('input',()=>{sourceFile=null;$('source-label').textContent='메일·텍스트 입력';updateTextLimit();clearConfirm();hideResult();});
$('kind').addEventListener('change',()=>{clearConfirm();hideResult();});
$('day').addEventListener('change',async()=>{setBusy(true);try{await api('/api/clock',{day:Number($('day').value)});await refresh();notify('시연 시점을 변경했습니다. 만료된 승인 조건은 재검증이 필요합니다.');}catch(error){notify(error.message,'error');await refresh();}finally{setBusy(false);}});
$('upload').addEventListener('change',async()=>{
  const file=$('upload').files[0];if(!file)return;
  if(!/\.(pdf|txt)$/i.test(file.name)||file.size>5*1024*1024){notify('5MB 이하의 PDF 또는 TXT 파일을 선택하세요.','error');$('upload').value='';return;}
  clearConfirm();hideResult();
  if(/\.txt$/i.test(file.name)){
    sourceFile=null;
    try{$('request').value=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());}
    catch{$('request').value='';$('upload').value='';$('source-label').textContent='파일 읽기 실패';updateTextLimit();notify('TXT 파일을 읽지 못했습니다. UTF-8 인코딩으로 저장한 파일을 선택하세요.','error');return;}
  }
  else {const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);sourceFile={name:file.name,base64:btoa(binary)};$('request').value=`[PDF 파일 선택: ${file.name}]\n검증을 시작하면 텍스트를 추출합니다. 스캔 이미지 PDF는 지원하지 않습니다.`;}
  updateTextLimit();
  $('source-label').textContent=file.name;notify('파일을 불러왔습니다. 내용을 확인하고 검증을 시작하세요.');
});
$('run-form').addEventListener('submit',async event=>{
  event.preventDefault();if(busy||approvalStateUnverified)return;
  if(updateTextLimit()){notify('요청 텍스트는 UTF-8 기준 5MiB 이하로 줄여 주세요.','error');return;}
  if($('kind').value==='order'&&!current){notify('발주 재검증의 기준이 될 저장된 견적을 먼저 선택하세요.','error');return;}
  const confirmations={};document.querySelectorAll('[data-code]').forEach(el=>confirmations[el.dataset.code]=el.value.trim());
  const body={text:$('request').value,kind:$('kind').value,day:Number($('day').value),mode:$('mode').value,confirmations};
  if(current&&body.kind==='order'){body.quote_id=current.id;body.expected_version=current.version;}if(sourceFile)body.file=sourceFile;
  setBusy(true);hideResult();$('notice').hidden=true;
  try{
    const result=await api('/api/run',body);
    if(result.source_text)$('request').value=result.source_text;
    renderMissing(result.missing||[]);
    if(result.quote){clearConfirm();if(await showSavedQuote(result.quote))notify(result.message||'계산 근거와 승인 작업을 저장했습니다.','success');}
    else {hideResult();renderLogs(result.logs||[]);notify(result.message||'필수 사양을 확인한 뒤 다시 검증하세요.',result.status==='error'?'error':'');if(missing.length){$('confirmation').scrollIntoView({block:'nearest',behavior:'instant'});$('confirm-0')?.focus();}}
  }catch(error){notify(error.message,'error');try{await refresh();}catch{}}
  finally{setBusy(false);}
});
function selectQuote(id,version){const quote=state.quotes.find(q=>q.id===id&&q.version===version);if(!quote)return;sourceFile=null;clearConfirm();if(quote.source_text){$('request').value=quote.source_text;$('kind').value=quote.request?.source==='synthetic_order_change'?'order':'rfq';$('source-label').textContent=quote.source_name||'저장된 요청 원문';}updateTextLimit();renderQuote(quote);}
$('history').addEventListener('click',event=>{const button=event.target.closest('[data-quote]');if(button&&!busy)selectQuote(Number(button.dataset.quote),Number(button.dataset.version));});
$('saved-quote').addEventListener('change',()=>{const [id,version]=$('saved-quote').value.split(':').map(Number);selectQuote(id,version);});
$('result-content').addEventListener('click',async event=>{
  const button=event.target.closest('[data-approve]');if(!button||busy||approvalStateUnverified||!current)return;
  const optionId=button.dataset.approve;
  const agreement=Array.from(document.querySelectorAll('[data-agreement]')).find(el=>el.dataset.agreement===optionId)?.checked||false;
  approvalStateUnverified=true;
  setBusy(true);hideResult();
  try{
    const result=await api('/api/approve',{quote_id:current.id,option_id:optionId,day:Number($('day').value),expected_version:current.version,customer_delivery_agreed:agreement});
    cacheQuote(result.quote);
    approvalStateUnverified=false;
    renderQuote(result.quote);
    try{await refresh();}catch(error){notify('승인은 기록됐지만 최신 상태를 다시 읽지 못했습니다. 새로고침해 확인하세요.','error');return;}
    notify('선택한 조건을 내부 승인했습니다. 고객에게 자동 발송되지는 않습니다.','success');
  }catch(error){
    approvalStateUnverified=true;
    notify(error.message,'error');
    try{await refresh();const latest=state.quotes.filter(q=>q.id===current?.id).sort((a,b)=>b.version-a.version)[0];if(latest)renderQuote(latest);}catch{}
  }finally{setBusy(false);}
});
refresh().then(()=>{$('request').value=state.samples.rfq||'';updateTextLimit();if(!state.ai?.available)notify('로컬 AI 연결을 확인해 주세요. 규칙 모드는 AI 판단을 사용하지 않습니다.');}).catch(error=>notify(error.message,'error'));


let state=null,cfg=null,editingMappingId=null,remoteProjects=[],remoteFolderData=null,remoteProjectId='',remoteProjectName='',remoteSelected=null,remoteExpanded=new Set(),standaloneRemote=false;
const $=id=>document.getElementById(id);const qsa=s=>[...document.querySelectorAll(s)];
function esc(s){return (s??'').toString().replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function fmt(n){n=Number(n||0);if(!n)return'0 B';let u=['B','KB','MB','GB','TB'],i=Math.min(u.length-1,Math.floor(Math.log(n)/Math.log(1024)));return(n/1024**i).toFixed(i?1:0)+' '+u[i]}
function fmtSpeed(n){return n>0?fmt(n)+'/s':'0 B/s'}
function fmtEta(sec){if(!sec||sec<=0)return'—';sec=Math.round(sec);if(sec<60)return sec+'s';let m=Math.floor(sec/60),s=sec%60;if(m<60)return m+'m '+s+'s';let h=Math.floor(m/60);return h+'h '+(m%60)+'m'}
function relativeTime(v){if(!v)return'Never';let d=new Date(v),s=Math.max(0,(Date.now()-d.getTime())/1000);if(s<60)return Math.floor(s)+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
function toast(msg,bad=false){let t=$('toast');t.textContent=msg;t.className=bad?'show bad':'show';setTimeout(()=>t.className='',3300)}
function directionLabel(d){return d==='two_way'?'Two-Way':d==='download'?'Download Only':'Upload Only'}
function directionIcon(d){return d==='two_way'?'↔':d==='download'?'↓':'↑'}
function mappingById(id){return(cfg?.watch_folders||[]).find(w=>w.id===id)}
function mappingName(id){return mappingById(id)?.name||'Unknown Mapping'}
function activeJobs(){return(state?.jobs||[]).filter(j=>['uploading','downloading'].includes(j.status))}

function showView(name){qsa('.view').forEach(v=>v.classList.toggle('active',v.id==='view-'+name));qsa('.navitem').forEach(b=>b.classList.toggle('active',b.dataset.view===name));if(name==='remote')setTimeout(()=>{},0)}
qsa('.navitem').forEach(b=>b.addEventListener('click',()=>showView(b.dataset.view)));

document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){
    e.preventDefault();$('globalSearch').focus();$('globalSearch').select();renderSearchResults();return;
  }
  if(e.key==='Escape'){
    if(document.activeElement===$('globalSearch') || ($('globalSearch').value||'')){
      clearGlobalSearch();$('globalSearch').blur();return;
    }
    closeMappingEditor();closeRemoteBrowser();
  }
  if(e.key==='Enter' && document.activeElement===$('globalSearch')){
    let first=document.querySelector('#searchResults .searchresult');
    if(first){e.preventDefault();first.click()}
  }
});
$('globalSearch').addEventListener('input',()=>{renderMappings();renderTransfers();renderActivity();renderSearchResults();});
$('globalSearch').addEventListener('focus',renderSearchResults);
document.addEventListener('click',e=>{if(!$('globalSearchBox').contains(e.target))hideSearchResults()});


function searchTerm(){return ($('globalSearch').value||'').trim().toLowerCase()}
function searchHaystack(values){return values.filter(v=>v!==undefined&&v!==null).join(' ').toLowerCase()}

function globalSearchItems(){
  if(!state||!cfg)return[];
  const term=searchTerm();
  if(!term)return[];
  let results=[];
  for(const w of (cfg.watch_folders||[])){
    const hay=searchHaystack([w.name,w.path,w.destination_label,w.project_id,w.folder_id,directionLabel(w.direction),w.enabled?'active':'paused',w.include_subfolders?'recursive':'folder only']);
    if(hay.includes(term))results.push({kind:'mapping',id:w.id,title:w.name||'Unnamed Mapping',meta:`${directionLabel(w.direction)} · ${w.enabled?'Active':'Paused'}`,sub:`${w.path||''} → ${w.destination_label||''}`,glyph:'↔'});
  }
  for(const job of (state.jobs||[])){
    const map=mappingName(job.watch_id);
    const hay=searchHaystack([job.filename,job.path,job.destination,map,job.status,job.direction,job.message,job.remote_asset_id]);
    if(hay.includes(term)){
      const completed=['complete','failed'].includes(job.status);
      results.push({kind:completed?'activity':'transfer',id:job.id,title:job.filename||'Transfer',meta:`${map} · ${job.status}`,sub:job.message||job.destination||'',glyph:job.direction==='download'?'↓':'↑',updated:job.updated_at||job.created_at||''});
    }
  }
  const rank={transfer:0,mapping:1,activity:2};
  results.sort((a,b)=>rank[a.kind]!==rank[b.kind]?rank[a.kind]-rank[b.kind]:(a.kind==='mapping'?(a.title||'').localeCompare(b.title||''):new Date(b.updated||0)-new Date(a.updated||0)));
  return results.slice(0,18);
}

function renderSearchResults(){
  const box=$('searchResults'),clear=$('searchClear'),term=searchTerm();
  clear.classList.toggle('visible',!!term);
  if(!term){box.classList.add('hidden');box.innerHTML='';return}
  const results=globalSearchItems(),groups={transfer:[],mapping:[],activity:[]};
  for(const r of results)groups[r.kind].push(r);
  if(!results.length){
    box.innerHTML=`<div class="searchnoresults"><b>No results for “${esc($('globalSearch').value)}”</b><span>Search mapping names, paths, filenames, status, or Wiredrive destinations.</span></div>`;
    box.classList.remove('hidden');return;
  }
  const labels={transfer:'Active / Queued Transfers',mapping:'Mappings',activity:'Activity & History'};
  let out='';
  for(const kind of ['transfer','mapping','activity']){
    if(!groups[kind].length)continue;
    out+=`<div class="searchgroup"><div class="searchgrouptitle">${labels[kind]}</div>`;
    out+=groups[kind].map(r=>`<button class="searchresult" onclick="openSearchResult('${r.kind}','${esc(r.id)}')"><span class="searchglyph ${r.kind}">${esc(r.glyph)}</span><span class="searchresulttext"><b>${esc(r.title)}</b><small>${esc(r.meta)}</small><em>${esc(r.sub)}</em></span><span class="searcharrow">›</span></button>`).join('');
    out+='</div>';
  }
  box.innerHTML=out;box.classList.remove('hidden');
}

function hideSearchResults(){$('searchResults').classList.add('hidden')}
function clearGlobalSearch(){$('globalSearch').value='';$('searchClear').classList.remove('visible');hideSearchResults();if(state&&cfg){renderMappings();renderTransfers();renderActivity()}}
function openSearchResult(kind,id){hideSearchResults();showView(kind==='mapping'?'mappings':kind==='activity'?'activity':'transfers')}

async function refresh(){try{let r=await fetch('/api/state');state=await r.json();cfg=state.config;renderAll()}catch(e){console.error(e)}}
function renderAll(){renderConnection();renderMetrics();renderMappings();renderTransfers();renderActivity();renderSettings();renderFooter()}
function renderConnection(){let ready=!!state.credentials?.ready,account=state.account||'';$('sideConnState').textContent=ready?'Connected':'Not connected';$('sideAccount').textContent=account||'Wiredrive';$('sideHost').textContent=(cfg.wiredrive?.site_url||'wiredrive.com').replace(/^https?:\/\//,'');$('sideConnState').parentElement.classList.toggle('connected',ready);$('footerConnection').textContent=ready?'Connected':'Disconnected';$('footerConnection').previousElementSibling.classList.toggle('connected',ready);$('settingsConnDetail').textContent=ready?(state.credentials.message||'Connected'):'Connect this client to Wiredrive';if(account&&!$('wdUser').value)$('wdUser').value=account}
function renderMetrics(){let jobs=state.jobs||[],maps=cfg.watch_folders||[],active=maps.filter(m=>m.enabled),uploads=jobs.filter(j=>j.status==='uploading'),downloads=jobs.filter(j=>j.status==='downloading'),synced=Number(state.synced_total||0),up=uploads.reduce((n,j)=>n+Number(j.transfer_speed||0),0),down=downloads.reduce((n,j)=>n+Number(j.transfer_speed||0),0);$('metricMappings').textContent=maps.length;$('metricMappingNote').textContent=active.length?active.length+' active mapping'+(active.length===1?'':'s'):'No active mappings';$('metricUploads').textContent=uploads.length;$('metricUploadSpeed').textContent=fmtSpeed(up);$('metricDownloads').textContent=downloads.length;$('metricDownloadSpeed').textContent=fmtSpeed(down);$('metricSynced').textContent=synced.toLocaleString();$('navTransferCount').textContent=uploads.length+downloads.length;$('navTransferCount').style.display=(uploads.length+downloads.length)?'grid':'none';$('healthMappings').textContent=active.length;$('healthObservers').textContent=state.watching?.length||0;$('healthNetwork').textContent=fmtSpeed(up+down);$('nextCheck').textContent='Every '+(cfg.remote_check_seconds||15)+' seconds';$('footerNextCheck').textContent=(cfg.remote_check_seconds||15)+'s polling';$('footerUp').textContent=fmtSpeed(up);$('footerDown').textContent=fmtSpeed(down)}
function mappingCard(w){let st=state.mapping_stats?.[w.id]||{},letter=(w.name||'M').trim().charAt(0).toUpperCase(),status=w.enabled?'Active':'Paused';return`<div class="mappingcard"><button class="mappingmenu" onclick="editMapping('${esc(w.id)}')">•••</button><div class="mapidentity"><div class="mapavatar">${esc(letter)}</div><div><strong>${esc(w.name)}</strong><small>${esc(directionLabel(w.direction))}${w.include_subfolders?' · Recursive':''}</small></div></div><div class="pathblock"><label>LOCAL</label><strong title="${esc(w.path)}">${esc(w.path)}</strong><small>${st.local_exists?'Folder available':'Folder unavailable'}</small></div><div class="mapdirection">${directionIcon(w.direction)}</div><div class="pathblock"><label>REMOTE</label><strong title="${esc(w.destination_label)}">${esc(w.destination_label)}</strong><small>${st.synced||0} files synchronized</small></div><div class="mapstatus ${w.enabled?'':'off'}"><i></i>${status}</div><div class="mappingactions" style="grid-column:1/-1"><button onclick="syncMapping('${esc(w.id)}')">Sync Now</button><button onclick="editMapping('${esc(w.id)}')">Edit</button><button onclick="toggleMapping('${esc(w.id)}',${!w.enabled})">${w.enabled?'Pause':'Enable'}</button><button class="dangertext" onclick="deleteMapping('${esc(w.id)}')">Remove</button></div></div>`}
function mappingTableRow(w){let st=state.mapping_stats?.[w.id]||{};return`<div class="mappingtablerow"><div class="namecell"><strong>${esc(w.name)}</strong><small>${w.include_subfolders?'Recursive':'This folder only'}</small></div><div><span class="statuspill ${w.enabled?'':'off'}">${w.enabled?'● Active':'Paused'}</span></div><div class="directionbadge">${directionIcon(w.direction)} ${esc(directionLabel(w.direction))}</div><div class="localpathcell" title="${esc(w.path)}"><span>${esc(w.path)}</span></div><div class="remotecell" title="${esc(w.destination_label)}"><span>${esc(w.destination_label)}</span></div><div class="rowactions"><button onclick="editMapping('${esc(w.id)}')">Edit</button><button class="dangertext" onclick="deleteMapping('${esc(w.id)}')">Delete</button></div></div>`}
function renderMappings(){let term=($('globalSearch').value||'').toLowerCase(),maps=(cfg.watch_folders||[]).filter(w=>!term||[w.name,w.path,w.destination_label].join(' ').toLowerCase().includes(term));$('mappingSummary').textContent=(cfg.watch_folders||[]).filter(w=>w.enabled).length+' active mappings';$('dashboardMappings').innerHTML=maps.length?maps.slice(0,4).map(mappingCard).join(''):'<div class="empty">No mappings yet. Create your first local ↔ Wiredrive relationship.</div>';$('mappingTable').innerHTML=`<div class="mappingtablerow header"><div>Mapping</div><div>Status</div><div>Direction</div><div>Local Path</div><div>Remote Path</div><div></div></div>`+(maps.length?maps.map(mappingTableRow).join(''):'<div class="empty">No mappings configured.</div>')}
function transferRow(j){let active=['uploading','downloading'].includes(j.status),download=j.direction==='download',pct=Math.max(0,Math.min(100,Number(j.progress||0))),moved=Math.min(Number(j.bytes_transferred||0),Number(j.size||0));return`<div class="transferrow ${download?'download':''}"><div class="transfericon ${download?'download':''}">${download?'↓':'↑'}</div><div class="transfername"><strong>${esc(j.filename)}</strong><small>${esc(mappingName(j.watch_id))} · ${active?(download?'Downloading':'Uploading'):esc(j.status)}</small></div><div class="transferdetail"><div class="telemetry"><span>${pct}% · ${fmt(moved)} / ${fmt(j.size)}</span><span class="speed">${active?fmtSpeed(Number(j.transfer_speed||0)):j.status==='complete'?'Complete':esc(j.message)}</span><span>${active?'ETA '+fmtEta(Number(j.eta_seconds||0)):''}</span></div><div class="transferbar"><i style="width:${pct}%"></i></div></div><button class="transferclose" onclick="removeJob('${esc(j.id)}')">×</button></div>`}
function renderTransfers(){let term=($('globalSearch').value||'').toLowerCase(),jobs=(state.jobs||[]).filter(j=>!term||[j.filename,j.destination,mappingName(j.watch_id)].join(' ').toLowerCase().includes(term)),active=jobs.filter(j=>['uploading','downloading','waiting','ready'].includes(j.status));$('activeTransferLabel').textContent=active.filter(j=>['uploading','downloading'].includes(j.status)).length+' in progress';$('dashboardTransfers').innerHTML=active.length?active.slice(0,5).map(transferRow).join(''):'<div class="empty">No active transfers.</div>';$('allTransfers').innerHTML=jobs.length?jobs.map(transferRow).join(''):'<div class="empty">No transfer history yet.</div>'}
function activityItem(j){let down=j.direction==='download',complete=j.status==='complete',glyph=complete?(down?'↓':'↑'):'!';return`<div class="activityitem"><div class="activityglyph ${down?'download':''}">${glyph}</div><div class="activitytext"><strong>${complete?(down?'Downloaded':'Uploaded'):'Failed'}</strong><b>${esc(j.filename)}</b><small>${esc(mappingName(j.watch_id))}</small></div><div class="activitytime">${relativeTime(j.updated_at)}</div></div>`}
function renderActivity(){let term=searchTerm(),jobs=(state.jobs||[]).filter(j=>['complete','failed'].includes(j.status)).filter(j=>!term||searchHaystack([j.filename,j.path,j.destination,mappingName(j.watch_id),j.status,j.direction,j.message,j.remote_asset_id]).includes(term));$('activityStream').innerHTML=jobs.length?jobs.slice(0,6).map(activityItem).join(''):'<div class="empty">'+(term?'No matching activity.':'No recent activity.')+'</div>';$('activityList').innerHTML=jobs.length?jobs.map(j=>`<div class="activityrow"><div class="activityglyph ${j.direction==='download'?'download':''}">${j.direction==='download'?'↓':'↑'}</div><div><strong>${j.status==='complete'?(j.direction==='download'?'Downloaded':'Uploaded'):'Failed'}</strong></div><div><b>${esc(j.filename)}</b><small>${esc(mappingName(j.watch_id))}</small></div><div>${fmt(j.size)}</div><div>${relativeTime(j.updated_at)}</div></div>`).join(''):'<div class="empty">'+(term?'No matching activity.':'No activity yet.')+'</div>'}
function renderSettings(){if(document.activeElement!==$('settingRemoteCheck'))$('settingRemoteCheck').value=cfg.remote_check_seconds||15;if(document.activeElement!==$('settingStable'))$('settingStable').value=cfg.stability_seconds||5;if(document.activeElement!==$('settingMode'))$('settingMode').value=cfg.uploader_mode||'wiredrive'}
function renderFooter(){}

async function syncNow(){let r=await fetch('/api/sync-now',{method:'POST'}),d=await r.json();if(!r.ok)return toast(d.error||'Sync scan failed',true);let q=(d.summary||[]).reduce((n,x)=>n+(x.queued||0),0);toast(q?q+' remote file'+(q===1?'':'s')+' queued':'Sync scan complete');refresh()}
async function syncMapping(id){let r=await fetch('/api/mappings/'+encodeURIComponent(id)+'/sync',{method:'POST'}),d=await r.json();if(!r.ok)return toast(d.error||'Mapping sync failed',true);toast('Mapping scan complete');refresh()}
async function retry(id){await fetch('/api/jobs/'+id+'/retry',{method:'POST'});refresh()}
async function removeJob(id){await fetch('/api/jobs/'+id,{method:'DELETE'});refresh()}
async function clearCompleted(){let done=(state.jobs||[]).filter(j=>['complete','failed'].includes(j.status));await Promise.all(done.map(j=>fetch('/api/jobs/'+j.id,{method:'DELETE'})));toast('Completed transfers cleared');refresh()}

function openMappingEditor(id=null){editingMappingId=id;let w=id?mappingById(id):null;$('mappingModalTitle').textContent=w?'Edit Mapping':'New Mapping';$('saveMappingButton').textContent=w?'Save Mapping':'Create Mapping';$('deleteMappingButton').style.display=w?'inline-flex':'none';$('mapName').value=w?.name||'';$('mapPath').value=w?.path||'';$('mapDestination').value=w?.destination_label||'';$('mapProjectId').value=w?.project_id||'';$('mapFolderId').value=w?.folder_id||'';$('mapDirection').value=w?.direction||'two_way';$('mapRecursive').checked=w?.include_subfolders!==false;$('mapCreateMissing').checked=w?.create_missing_folders!==false;$('mapEnabled').checked=w?.enabled!==false;$('mappingModal').classList.remove('hidden')}
function editMapping(id){openMappingEditor(id)}function closeMappingEditor(){$('mappingModal').classList.add('hidden')}
async function browseFolderForMapping(){let r=await fetch('/api/browse-folder',{method:'POST'}),d=await r.json();if(d.cancelled)return;if(!r.ok)return toast(d.error||'Folder picker failed',true);$('mapPath').value=d.path||'';if(!$('mapName').value)$('mapName').value=(d.path||'').split('/').filter(Boolean).pop()||'Wiredrive Mapping'}
async function saveMapping(){let payload={name:$('mapName').value.trim(),path:$('mapPath').value.trim(),destination_label:$('mapDestination').value.trim(),project_id:$('mapProjectId').value.trim(),folder_id:$('mapFolderId').value.trim(),direction:$('mapDirection').value,include_subfolders:$('mapRecursive').checked,create_missing_folders:$('mapCreateMissing').checked,enabled:$('mapEnabled').checked},url=editingMappingId?'/api/mappings/'+encodeURIComponent(editingMappingId):'/api/mappings',method=editingMappingId?'PUT':'POST';let b=$('saveMappingButton');b.disabled=true;try{let r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok)return toast(d.error||'Could not save mapping',true);closeMappingEditor();toast(editingMappingId?'Mapping updated':'Mapping created');refresh()}finally{b.disabled=false}}
async function toggleMapping(id,enabled){let r=await fetch('/api/mappings/'+encodeURIComponent(id)+'/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}),d=await r.json();if(!r.ok)return toast(d.error||'Could not update mapping',true);refresh()}
async function deleteMapping(id){let w=mappingById(id);if(!confirm('Delete mapping "'+(w?.name||id)+'"?\n\nThis removes the mapping and its local sync history only. It will NOT delete local files or Wiredrive files.'))return false;let r=await fetch('/api/mappings/'+encodeURIComponent(id),{method:'DELETE'}),d=await r.json();if(!r.ok){toast(d.error||'Could not delete mapping',true);return false}toast('Mapping deleted');await refresh();return true}
async function deleteEditingMapping(){if(!editingMappingId)return;let ok=await deleteMapping(editingMappingId);if(ok){editingMappingId=null;closeMappingEditor()}}

async function connectWiredrive(){let username=$('wdUser').value.trim(),password=$('wdPass').value;if(!username||!password)return toast('Enter your Wiredrive email and password',true);let r=await fetch('/api/wiredrive/connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})}),d=await r.json();$('wdPass').value='';if(!r.ok)return toast(d.error||'Wiredrive login failed',true);toast('Connected to Wiredrive');refresh()}
async function disconnectWiredrive(){let r=await fetch('/api/wiredrive/disconnect',{method:'POST'});if(r.ok){toast('Disconnected');refresh()}}
async function refreshCredentials(){let r=await fetch('/api/refresh-credentials',{method:'POST'}),d=await r.json();if(!r.ok)return toast(d.error||'Credential refresh failed',true);toast('Wiredrive session refreshed');refresh()}
async function importHar(){let f=$('harFile').files[0];if(!f)return toast('Choose a HAR file first',true);let fd=new FormData();fd.append('har',f);let r=await fetch('/api/import-har',{method:'POST',body:fd}),d=await r.json();if(!r.ok)return toast(d.error||'HAR import failed',true);$('harFile').value='';toast('HAR imported');refresh()}
async function saveSettings(){let payload={remote_check_seconds:Number($('settingRemoteCheck').value||15),stability_seconds:Number($('settingStable').value||5),uploader_mode:$('settingMode').value};let r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();if(!r.ok)return toast(d.error||'Settings save failed',true);toast('Settings saved');refresh()}

function showRemoteError(msg){$('folderTree').innerHTML='<div class="browsererror">'+esc(msg)+'</div>';toast(msg,true)}
async function openRemoteBrowser(standalone=false){standaloneRemote=!!standalone;$('remoteBrowserModal').classList.remove('hidden');$('remoteSelection').textContent='No folder selected';$('useRemoteBtn').disabled=true;$('folderTree').innerHTML='<div class="browserempty">Loading Wiredrive projects…</div>';try{let r=await fetch('/api/wiredrive/projects'),d=await r.json();if(!r.ok)throw new Error(d.error||'Could not load projects');remoteProjects=d.projects||[];renderRemoteProjects();let initial=standalone?'':$('mapProjectId').value.trim();if(!initial||!remoteProjects.some(p=>p.id===initial))initial=remoteProjects[0]?.id||'';if(initial)await loadRemoteProject(initial);else showRemoteError('No Wiredrive projects available.')}catch(e){showRemoteError(e.message)}}
function closeRemoteBrowser(){$('remoteBrowserModal').classList.add('hidden')}
function renderRemoteProjects(){let q=($('projectSearch').value||'').toLowerCase(),items=remoteProjects.filter(p=>!q||p.name.toLowerCase().includes(q));$('projectList').innerHTML=items.length?items.map(p=>`<button class="projectrow ${p.id===remoteProjectId?'active':''}" onclick="loadRemoteProject('${esc(p.id)}')">${esc(p.name)}</button>`).join(''):'<div class="browserempty">No matching projects</div>'}
async function loadRemoteProject(pid){remoteProjectId=String(pid);remoteProjectName=remoteProjects.find(p=>p.id===remoteProjectId)?.name||'';renderRemoteProjects();$('folderTree').innerHTML='<div class="browserempty">Loading folders…</div>';$('remoteBreadcrumb').textContent=remoteProjectName;remoteSelected=null;$('useRemoteBtn').disabled=true;let r=await fetch('/api/wiredrive/folders?project_id='+encodeURIComponent(pid)),d=await r.json();if(!r.ok)return showRemoteError(d.error||'Could not load folders');remoteFolderData=d;remoteProjectName=d.project_name||remoteProjectName;remoteExpanded=new Set([String(d.root_id)]);renderRemoteFolders()}
async function reloadRemoteProject(){if(remoteProjectId)await loadRemoteProject(remoteProjectId)}
function remoteFolderMaps(){let folders=remoteFolderData?.folders||[],byId=new Map(folders.map(f=>[String(f.id),f])),children=new Map();for(let f of folders){let p=String(f.parent||remoteProjectId);if(!children.has(p))children.set(p,[]);children.get(p).push(f)}for(let a of children.values())a.sort((x,y)=>x.name.localeCompare(y.name,undefined,{numeric:true}));return{byId,children}}
function remotePath(folderId){let{byId}=remoteFolderMaps(),parts=[],cur=String(folderId||remoteProjectId),guard=0;while(cur&&cur!==String(remoteProjectId)&&guard++<100){let f=byId.get(cur);if(!f)break;parts.unshift(f.name);cur=String(f.parent||'')}parts.unshift(remoteProjectName||'Project '+remoteProjectId);return parts}
function selectRemoteFolder(id,name){remoteSelected={project_id:String(remoteProjectId),folder_id:String(id),path:remotePath(id)};$('remoteSelection').textContent=remoteSelected.path.join(' / ');$('remoteBreadcrumb').textContent=remoteSelected.path.join(' / ');$('useRemoteBtn').disabled=false;renderRemoteFolders()}
function toggleRemoteFolder(id,e){e?.stopPropagation();id=String(id);remoteExpanded.has(id)?remoteExpanded.delete(id):remoteExpanded.add(id);renderRemoteFolders()}
function renderRemoteFolders(){if(!remoteFolderData)return;let q=($('folderSearch').value||'').toLowerCase(),{children}=remoteFolderMaps(),selected=remoteSelected?.folder_id||'';if(q){let hits=(remoteFolderData.folders||[]).filter(f=>f.name.toLowerCase().includes(q)).slice(0,250);$('folderTree').innerHTML=hits.length?hits.map(f=>`<button class="foldersearchrow ${selected===String(f.id)?'selected':''}" onclick="selectRemoteFolder('${esc(f.id)}','${esc(f.name)}')"><span class="folderglyph">▰</span><span><b>${esc(f.name)}</b><small>${esc(remotePath(f.id).slice(0,-1).join(' / '))}</small></span></button>`).join(''):'<div class="browserempty">No matching folders</div>';return}function branch(parent,depth){return(children.get(String(parent))||[]).map(f=>{let id=String(f.id),kids=(children.get(id)||[]).length>0,open=remoteExpanded.has(id);return`<div><div class="folderrow ${selected===id?'selected':''}" style="--depth:${depth}" onclick="selectRemoteFolder('${esc(id)}','${esc(f.name)}')"><button class="twisty" onclick="toggleRemoteFolder('${esc(id)}',event)">${kids?(open?'▾':'▸'):''}</button><span class="folderglyph">▰</span><span class="foldername">${esc(f.name)}</span>${f.count?`<span class="foldercount">${f.count}</span>`:''}</div>${kids&&open?branch(id,depth+1):''}</div>`}).join('')}let root=String(remoteProjectId);$('folderTree').innerHTML=`<div class="folderrow ${selected===root?'selected':''}" style="--depth:0" onclick="selectRemoteFolder('${esc(root)}','${esc(remoteProjectName)}')"><button class="twisty" onclick="toggleRemoteFolder('${esc(root)}',event)">${remoteExpanded.has(root)?'▾':'▸'}</button><span class="folderglyph projectglyph">◆</span><span class="foldername">${esc(remoteProjectName)}</span></div>${remoteExpanded.has(root)?branch(root,1):''}`}
function useRemoteSelection(){if(!remoteSelected)return;if(standaloneRemote){closeRemoteBrowser();openMappingEditor();setTimeout(()=>{$('mapProjectId').value=remoteSelected.project_id;$('mapFolderId').value=remoteSelected.folder_id;$('mapDestination').value=remoteSelected.path.join(' / ')},0);return}$('mapProjectId').value=remoteSelected.project_id;$('mapFolderId').value=remoteSelected.folder_id;$('mapDestination').value=remoteSelected.path.join(' / ');closeRemoteBrowser()}
$('projectSearch').addEventListener('input',renderRemoteProjects);$('folderSearch').addEventListener('input',renderRemoteFolders);$('mappingModal').addEventListener('click',e=>{if(e.target===$('mappingModal'))closeMappingEditor()});$('remoteBrowserModal').addEventListener('click',e=>{if(e.target===$('remoteBrowserModal'))closeRemoteBrowser()});

refresh();setInterval(refresh,1500);

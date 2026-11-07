let token = sessionStorage.getItem('yml-token') || '';
let state = null;
let libraryPreview = null;
let directoryState = null;
const $ = id => document.getElementById(id);
const labels = {available:'Disponible',queued:'En cola',downloading:'Descargando',moving:'Trasladando',downloaded:'Guardado',error:'Error'};
const active = ['queued','downloading','moving'];
function node(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
async function api(path,body){
  const response=await fetch(path,{method:body?'POST':'GET',headers:{...(body?{'Content-Type':'application/json'}:{}),...(token?{Authorization:'Bearer '+token}:{})},...(body?{body:JSON.stringify(body)}:{})});
  const result=await response.json();
  if(response.status===401)$('auth').hidden=false;
  if(!response.ok)throw Error(result.error||'No se pudo completar la operación.');
  return result;
}
function notice(message){$('notice').hidden=!message;$('notice').textContent=message||'';}
function render(){
  if(!state)return;
  const all=state.episodes;
  const filtered=all.filter(e=>{switch($('filter').value){case'active':return active.includes(e.status);case'available':return e.status!=='downloaded';case'downloaded':return e.status==='downloaded';case'error':return e.status==='error';default:return true;}});
  $('total').textContent=all.length;
  $('saved').textContent=state.library?.count ?? all.filter(e=>e.status==='downloaded').length;
  $('season').textContent=state.season;
  $('quality').textContent=state.max_height+'p';
  $('folder').textContent=state.output_dir;
  $('schedule').textContent=!state.settings?.configured?'Configura la carpeta en Ajustes':state.auto_download?`Automático · cada ${state.check_interval_minutes} min`:'Descarga manual';
  $('checked').textContent=state.scanning?'Revisando capítulos…':state.last_scan?'Última revisión: '+new Date(state.last_scan).toLocaleString('es-ES',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'Sin revisiones';
  $('scan').disabled=state.scanning;
  if(document.activeElement!==$('parallel'))$('parallel').value=String(state.download_workers||3);
  $('settings-save').disabled=!libraryPreview||state.scanning||all.some(e=>active.includes(e.status));
  $('episodes').replaceChildren();
  if(!filtered.length){const row=node('tr');const cell=node('td',state.scanning?'Buscando capítulos…':'No hay capítulos.','empty');cell.colSpan=5;row.append(cell);$('episodes').append(row);}
  for(const e of filtered){
    const row=node('tr');row.append(node('td',String(e.number).padStart(2,'0'),'number'));
    const title=node('td',undefined,'title');
    const shortTitle=e.title.replace(/^Capítulo\s+\d+\s+Yo\s+Me\s+Llamo\s*:\s*/i,'');
    const link=node('a',shortTitle);link.href=e.url;link.target='_blank';link.rel='noopener noreferrer';title.append(link);row.append(title);
    row.append(node('td',e.published?new Date(e.published).toLocaleDateString('es-ES',{timeZone:'America/Bogota',day:'2-digit',month:'2-digit',year:'2-digit'}):'—','date'));
    const status=node('td',labels[e.status]||e.status,'status');status.dataset.state=e.status;
    if(['downloading','moving'].includes(e.status)){
      status.firstChild.textContent=e.status==='moving'?'Trasladando…':`Descargando · ${e.progress} %`;
      const progress=node('progress');progress.max=100;progress.value=e.progress;progress.setAttribute('aria-label','Progreso del capítulo '+e.number);status.append(progress);
    }
    if(e.error)status.append(node('small',e.error));
    row.append(status);
    const action=node('td',undefined,'action');
    if(!active.includes(e.status)&&e.status!=='downloaded'){
      const button=node('button',e.status==='error'?'Reintentar':'Descargar');
      button.disabled=!state.settings?.configured;
      button.setAttribute('aria-label',(e.status==='error'?'Reintentar':'Descargar')+' capítulo '+e.number);
      button.onclick=async()=>{button.disabled=true;try{await api('/api/download',{url:e.url});await refresh();}catch(err){notice(err.message);button.disabled=false;}};
      action.append(button);
    }
    row.append(action);$('episodes').append(row);
  }
}
async function refresh(){try{state=await api('/api/status');$('auth').hidden=true;notice(state.error||state.library_error);render();}catch(err){notice(err.message);$('checked').textContent='Sin conexión';}}
$('scan').onclick=async()=>{try{$('scan').disabled=true;await api('/api/scan',{});await refresh();}catch(err){notice(err.message);$('scan').disabled=false;}};
$('filter').onchange=render;
$('auth').onsubmit=e=>{e.preventDefault();token=$('token').value;sessionStorage.setItem('yml-token',token);$('token').value='';refresh();};
refresh();setInterval(refresh,3000);

function settingsError(message){$('settings-error').hidden=!message;$('settings-error').textContent=message||'';}
function invalidatePreview(){libraryPreview=null;$('library-preview').replaceChildren();$('settings-save').disabled=true;}
async function inspectDestination(){
  settingsError('');invalidatePreview();$('inspect-library').disabled=true;
  try{
    libraryPreview=await api('/api/settings/inspect',{output_dir:$('destination').value});
    $('destination').value=libraryPreview.output_dir;
    $('start-episode').value=libraryPreview.last_episode?libraryPreview.last_episode+1:(state?.settings?.min_episode||75);
    const summary=node('div',`${libraryPreview.count} ${libraryPreview.count===1?'capítulo reconocido':'capítulos reconocidos'} · Último: ${libraryPreview.last_episode||'ninguno'}`);
    $('library-preview').append(summary);
    if(libraryPreview.episodes.length){const list=node('ul');for(const file of libraryPreview.episodes.slice(-5))list.append(node('li',file.name));$('library-preview').append(list);}
    if(libraryPreview.unrecognized_count){const details=node('details');details.append(node('summary',`${libraryPreview.unrecognized_count} archivos sin reconocer`));const list=node('ul');for(const name of libraryPreview.unrecognized)list.append(node('li',name));details.append(list);$('library-preview').append(details);}
    $('settings-save').disabled=Boolean(state?.scanning||state?.episodes.some(e=>active.includes(e.status)));
  }catch(error){settingsError(error.message);}finally{$('inspect-library').disabled=false;}
}
async function browseDirectory(path){
  settingsError('');
  try{
    directoryState=await api('/api/directories'+(path?'?path='+encodeURIComponent(path):''));
    $('directory-browser').hidden=false;$('directory-path').textContent=directoryState.path;
    $('directory-up').disabled=!directoryState.parent;$('directory-folders').replaceChildren();
    for(const folder of directoryState.folders){const button=node('button',folder.name);button.type='button';button.onclick=()=>browseDirectory(folder.path);$('directory-folders').append(button);}
    if(!directoryState.folders.length)$('directory-folders').append(node('p','Sin subcarpetas.'));
  }catch(error){settingsError(error.message);}
}
$('settings-open').onclick=async()=>{
  if(!state){notice('Introduce el token para abrir Ajustes.');return;}
  settingsError('');$('destination').value=state.settings.output_dir;$('interval').value=state.check_interval_minutes;
  $('automatic').checked=state.auto_download;$('start-episode').value=state.settings.resume_after+1;
  $('directory-browser').hidden=true;$('settings-dialog').showModal();await inspectDestination();
};
$('settings-close').onclick=()=>$('settings-dialog').close();
$('destination').oninput=invalidatePreview;
$('inspect-library').onclick=inspectDestination;
$('browse').onclick=()=>browseDirectory();
$('directory-up').onclick=()=>directoryState?.parent&&browseDirectory(directoryState.parent);
$('directory-choose').onclick=async()=>{if(!directoryState)return;$('destination').value=directoryState.path;$('directory-browser').hidden=true;await inspectDestination();};
$('settings-form').onsubmit=async event=>{
  event.preventDefault();settingsError('');
  if(!libraryPreview||libraryPreview.output_dir!==$('destination').value){settingsError('Busca primero los capítulos de esta carpeta.');return;}
  $('settings-save').disabled=true;
  try{
    await api('/api/settings',{output_dir:libraryPreview.output_dir,start_episode:Number($('start-episode').value),check_interval_minutes:Number($('interval').value),auto_download:$('automatic').checked});
    $('settings-dialog').close();await refresh();
  }catch(error){settingsError(error.message);$('settings-save').disabled=false;}
};

$('parallel').onchange=async()=>{try{await api('/api/concurrency',{download_workers:Number($('parallel').value)});await refresh();}catch(error){notice(error.message);}};

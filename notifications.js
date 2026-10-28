let notificationTimer=null,notificationInFlight=false,notificationRevision=0;
let notificationData={notifications:[],unread:0},notificationSnapshot='';
let pendingRepairLink=new URLSearchParams(location.search).get('job');

function stopNotifications(){
  clearInterval(notificationTimer);notificationTimer=null;notificationRevision++;
  notificationData={notifications:[],unread:0};notificationSnapshot='';
  $('#notification-panel').hidden=true;$('#notification-toggle').setAttribute('aria-expanded','false');
  $('#notification-toggle').setAttribute('aria-label','Notifications, 0 unread');
  $('#notification-count').textContent='0';$('#notification-list').replaceChildren();$('#email-delivery-status').replaceChildren();
}

function startNotifications(){
  stopNotifications();void refreshNotifications();
  notificationTimer=setInterval(refreshNotifications,5000);
}

async function refreshNotifications(){
  if(!currentUser||$('#app-shell').hidden||document.hidden||notificationInFlight)return;
  const userId=currentUser.id,revision=notificationRevision;
  notificationInFlight=true;
  try{
    const data=await apiRequest('/api/notifications');
    if(currentUser?.id!==userId||revision!==notificationRevision)return;
    const snapshot=JSON.stringify(data);
    if(snapshot===notificationSnapshot)return;
    const known=new Set(notificationData.notifications.map(item=>item.id));
    const newest=data.notifications.find(item=>!known.has(item.id)&&!item.read_at);
    if(notificationSnapshot&&newest)toast(newest.title+' · '+newest.job_id);
    notificationData=data;notificationSnapshot=snapshot;renderNotifications();
  }catch{
    // Keep the saved inbox visible and retry when connected.
  }finally{notificationInFlight=false}
}

function renderNotifications(){
  $('#notification-count').textContent=String(notificationData.unread);
  $('#notification-toggle').setAttribute('aria-label',`Notifications, ${notificationData.unread} unread`);
  $('#notifications-read-all').disabled=notificationData.unread===0;
  $('#notification-list').innerHTML=notificationData.notifications.map(item=>
    `<article class="notification-item ${item.read_at?'':'unread'}"><button class="link notification-open" type="button" data-notification="${esc(item.id)}"><strong>${esc(item.title)}</strong><small>${esc(item.job_id)} · ${esc(new Date(item.created_at).toLocaleString())}${item.read_at?'':' · Unread'}</small></button><p>${esc(item.message)}</p></article>`
  ).join('')||'<div class="empty">No notifications yet. Repair updates will appear here.</div>';
  const delivery=notificationData.emailDelivery;
  const providerName=delivery?.provider==='brevo'?'Brevo':'Resend';
  $('#email-delivery-status').innerHTML=delivery?`<div class="note">${delivery.configured?`Email alerts enabled (${providerName}). ${delivery.pending} queued; ${delivery.failed} need retry.`:'Email alerts need setup. In-app alerts are active. Follow the email setup in DEPLOYMENT.md.'}${delivery.configured&&delivery.failed?' <button class="link" type="button" id="retry-notification-email">Retry failed emails</button>':''}</div>`:'';
  const retry=$('#retry-notification-email');if(retry)retry.onclick=retryNotificationEmail;
}

function openRepairFromAlert(id){
  const job=get(id);
  if(!job){toast('This repair is not available to your account.');return}
  if(currentRole==='customer'){
    navigate('track');const form=$('#tracking');form.elements.id.value=id;form.requestSubmit();
    $('#result').scrollIntoView({block:'nearest'});
  }else{navigate('jobs');detail(id)}
}

function openPendingRepairLink(){
  if(!pendingRepairLink)return;
  const id=pendingRepairLink;pendingRepairLink=null;
  openRepairFromAlert(id);
}

$('#notification-toggle').onclick=()=>{
  const panel=$('#notification-panel');panel.hidden=!panel.hidden;
  $('#notification-toggle').setAttribute('aria-expanded',String(!panel.hidden));
  if(!panel.hidden){renderNotifications();void refreshNotifications()}
};
$('#notification-list').onclick=async event=>{
  const button=event.target.closest('[data-notification]');if(!button)return;
  const item=notificationData.notifications.find(value=>value.id===button.dataset.notification);if(!item)return;
  const userId=currentUser?.id,revision=notificationRevision;
  try{
    await apiRequest('/api/notifications/read',{method:'POST',body:JSON.stringify({id:item.id})});
    if(currentUser?.id!==userId||revision!==notificationRevision)return;
    const data=await apiRequest('/api/workspace');
    if(currentUser?.id!==userId||revision!==notificationRevision)return;
    workspaceMutationVersion++;currentUser=data.user;teamMembers=data.technicians||[];editableJobIds=data.editableJobIds||[];
    db={jobs:data.jobs||[],parts:data.parts||[],activity:data.activity||[],version:data.version||0};
    $('#notification-panel').hidden=true;$('#notification-toggle').setAttribute('aria-expanded','false');
    openRepairFromAlert(item.job_id);void refreshNotifications();
  }catch(error){toast(error.message)}
};
$('#notifications-read-all').onclick=async()=>{
  try{await apiRequest('/api/notifications/read',{method:'POST',body:'{}'});await refreshNotifications()}catch(error){toast(error.message)}
};
async function retryNotificationEmail(){
  try{await apiRequest('/api/notifications/retry-email',{method:'POST',body:'{}'});toast('Failed emails queued for retry.');await refreshNotifications()}catch(error){toast(error.message)}
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refreshNotifications()});
window.addEventListener('focus',()=>void refreshNotifications());
initializeAuth();

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'fixtrack-browser-'));
const base = 'http://127.0.0.1:5087';
const python = spawn(process.env.PYTHON || 'python', ['-u', '-c', `
import os
from pathlib import Path
import server
server.DATABASE = Path(os.environ['TEST_DATABASE'])
server.init_db()
server.app.run(host='127.0.0.1',port=5087,threaded=True,use_reloader=False)
`], {cwd: path.resolve(__dirname, '..'), env: {...process.env, DATABASE_URL:'', FIXTRACK_REQUIRE_POSTGRES:'0', FIXTRACK_COOKIE_SECURE:'0', FIXTRACK_SETUP_TOKEN:'', SECRET_KEY:'isolated-browser-test-key', TEST_DATABASE:path.join(temporary,'test.sqlite3')}, stdio:['ignore','ignore','pipe']});
let logs='';python.stderr.on('data',data=>logs+=data);
let browser;
async function waitFor(check, description) {
  const deadline=Date.now()+16000;
  while(Date.now()<deadline){if(await check())return;await new Promise(resolve=>setTimeout(resolve,150))}
  throw new Error('Timed out: '+description);
}
async function post(context,url,data,status=200){const response=await context.request.post(base+url,{data});assert.equal(response.status(),status,await response.text());return response.json()}
(async()=>{
  await waitFor(async()=>{try{return (await fetch(base+'/healthz')).ok}catch{return false}},'test server');
  browser=await chromium.launch({executablePath:process.env.CHROME,headless:true});
  const admin=await browser.newContext(),customer=await browser.newContext();
  const errors=[];
  const adminPage=await admin.newPage(),customerPage=await customer.newPage();
  for(const page of [adminPage,customerPage])page.on('pageerror',error=>errors.push(error.message));
  await post(admin,'/api/setup',{name:'Browser Admin',email:'browser-admin@example.test',password:'Browser-admin-password-42'},201);
  await post(customer,'/api/signup',{name:'Browser Customer',email:'browser-customer@example.test',password:'Browser-customer-password-42'},202);
  const users=await (await admin.request.get(base+'/api/users')).json();
  await post(admin,`/api/users/${users.users.find(user=>user.role==='customer').id}/verify`,{});
  await post(customer,'/api/login',{email:'browser-customer@example.test',password:'Browser-customer-password-42'});
  const {job}=await post(admin,'/api/jobs',{customer:'Browser Customer',customerEmail:'browser-customer@example.test',device:'Sync test phone',fault:'Broken screen'},201);
  await post(admin,'/api/jobs',{customer:'Other Customer',customerEmail:'other@example.test',device:'Private other phone',fault:'Broken battery'},201);
  await Promise.all([adminPage.goto(base),customerPage.goto(base)]);
  const card=customerPage.locator(`[data-repair-id="${job.id}"]`);
  await card.waitFor();
  assert.equal(await customerPage.getByText('Private other phone').count(),0);
  assert.equal(await customerPage.getByRole('button',{name:'Approve quotation'}).count(),0);
  await adminPage.locator('nav button[data-view="jobs"]').click();
  await adminPage.getByRole('button',{name:job.id,exact:true}).click();
  await adminPage.getByRole('button',{name:'Create quotation',exact:true}).click();
  await adminPage.locator('#quoteform [name="quote"]').fill('300');
  await adminPage.locator('#quoteform [name="work"]').fill('Replace screen');
  await adminPage.getByRole('button',{name:'Save quotation',exact:true}).click();
  await customerPage.getByRole('button',{name:'Approve quotation'}).waitFor({timeout:16000});
  console.log('PASS: Admin quotation appears in customer session without reload.');
  await customerPage.locator('#tracking [name="id"]').fill(job.id);
  await customerPage.locator('#tracking button').click();
  await post(admin,`/api/jobs/${job.id}/quotation`,{quote:350,work:'Updated quote'});
  await waitFor(async()=>/350/.test(await customerPage.locator('#result .quoteamount').innerText()),'selected repair quote refresh');
  assert.equal(await customerPage.locator('#tracking [name="id"]').inputValue(),job.id);
  await customerPage.locator('#result').getByRole('button',{name:'Approve quotation'}).click();
  await adminPage.locator('nav button[data-view="quotes"]').click();
  await waitFor(async()=>await adminPage.locator('.card .badge').first().innerText()==='Approved','approval reaches admin');
  console.log('PASS: Selected repair stays selected; customer approval reaches admin automatically.');
  await adminPage.evaluate(()=>newJob());
  await adminPage.locator('#newjob [name="customer"]').fill('Unfinished customer name');
  await post(admin,`/api/jobs/${job.id}/quotation`,{quote:400,work:'Another quote'});
  await waitFor(async()=>/400/.test(await customerPage.locator('#result .quoteamount').innerText()),'customer sees new pending quote');
  assert.equal(await adminPage.locator('#newjob [name="customer"]').inputValue(),'Unfinished customer name');
  assert.equal(await adminPage.locator('#modal').evaluate(dialog=>dialog.open),true);
  await adminPage.locator('#modal .close').click();
  await customerPage.locator('#result').getByRole('button',{name:'Decline quotation'}).click();
  await waitFor(async()=>await adminPage.locator('.card .badge').first().innerText()==='Declined','decline reaches admin');
  console.log('PASS: Open forms retain edits; decline reaches admin automatically.');
  // Delay a refresh until after a local mutation: its old snapshot must be ignored.
  await adminPage.evaluate(()=>navigate('jobs'));
  await adminPage.locator('#content input[aria-label="Search jobs"]').fill('Sync test');
  await adminPage.locator('#crumb').click();
  await post(admin,`/api/jobs/${job.id}/quotation`,{quote:450,work:'Search preservation quote'});
  await waitFor(async()=>await adminPage.evaluate(()=>db.jobs.some(j=>j.quote===450)),'admin refresh');
  assert.equal(await adminPage.locator('#content input[aria-label="Search jobs"]').inputValue(),'Sync test');
  assert.equal(await adminPage.locator('#jobrows tr').count(),1);
  console.log('PASS: Background refresh preserves job search.');
  let releaseSnapshot, snapshotReady;
  const snapshotCaptured=new Promise(resolve=>snapshotReady=resolve);
  const release=new Promise(resolve=>releaseSnapshot=resolve);
  let intercepted=false;
  await adminPage.route('**/api/workspace',async route=>{
    if(route.request().method()!=='GET'||intercepted)return route.continue();
    intercepted=true;
    const response=await route.fetch();snapshotReady();
    await release;await route.fulfill({response});
  });
  await adminPage.evaluate(()=>{void refreshWorkspace()});
  await snapshotCaptured;
  await adminPage.getByRole('button',{name:job.id,exact:true}).click();
  await adminPage.getByRole('button',{name:'Edit quotation',exact:true}).click();
  await adminPage.locator('#quoteform [name="quote"]').fill('500');
  await adminPage.getByRole('button',{name:'Save quotation',exact:true}).click();
  await waitFor(async()=>await adminPage.evaluate(()=>pendingWorkspaceSaves===0),'local quotation saved');
  releaseSnapshot();
  await waitFor(async()=>await adminPage.evaluate(()=>!workspaceRefreshInFlight),'old refresh completed');
  assert.equal(await adminPage.evaluate(()=>db.jobs.find(j=>j.device==='Sync test phone').quote),500);
  await adminPage.unroute('**/api/workspace');
  console.log('PASS: Delayed refresh cannot overwrite a newer local quotation.');
  await adminPage.locator('nav button[data-view="accounts"]').click();
  await post(customer,'/api/signup',{name:'New Pending Customer',email:'new-pending@example.test',password:'New-pending-password-42'},202);
  await adminPage.getByText('New Pending Customer',{exact:true}).waitFor({timeout:16000});
  console.log('PASS: Newly registered customers appear in admin accounts automatically.');
  await customerPage.locator('#logout-button').click();
  await waitFor(async()=>await customerPage.locator('#app-shell').isHidden(),'logout');
  assert.equal(await customerPage.evaluate(()=>workspaceRefreshTimer),null);
  assert.equal(await customerPage.locator('#auth-screen').isVisible(),true);
  assert.deepEqual(errors,[]);
  console.log('PASS: Refresh stops on logout; no browser JavaScript errors.');
})().catch(error=>{console.error(error);process.exitCode=1}).finally(async()=>{
  if(browser)await browser.close();
  python.kill();
  await new Promise(resolve=>python.once('exit',resolve));
  fs.rmSync(temporary,{recursive:true,force:true});
});

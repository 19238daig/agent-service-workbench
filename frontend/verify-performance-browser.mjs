/** Authenticated read-only UI review; never persist tokens, passwords or traces. */
import { chromium } from '@playwright/test';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
const output='../.local/performance';await mkdir(output,{recursive:true});
const account=JSON.parse(await readFile('../.local/auth/bootstrap-accounts.json','utf8')).accounts.find(a=>a.login_name==='admin');
const browser=await chromium.launch({channel:'msedge',headless:true});
const context=await browser.newContext({baseURL:'http://127.0.0.1:8000',viewport:{width:1440,height:1000}});
const report={passed:false,real_browser:true,real_api:true,sizes:[]};let headers;
try {
  await context.addInitScript(({id,key})=>{if(localStorage.getItem('perf-initialized'))return;localStorage.setItem('perf-initialized','1');const c={id:key,title:'第25步页面验收',createdAt:Date.now(),updatedAt:Date.now(),messages:[],draft:''};localStorage.setItem(`smart-cs.workspace.v2:${id}`,JSON.stringify({userId:id,activeId:key,conversations:[c]}));},{id:account.user_id,key:randomUUID()});
  report.stage='login';const page=await context.newPage();page.setDefaultTimeout(30000);await page.goto('/');
  await page.getByLabel('账号',{exact:true}).fill('admin');await page.getByLabel('密码',{exact:true}).fill(account.password);
  const response=page.waitForResponse('**/api/auth/login');await page.getByRole('button',{name:'登录工作台'}).click();
  const login=await(await response).json();headers={Authorization:`Bearer ${login.access_token}`};
  report.stage='navigate';await page.getByRole('button',{name:'运行观测',exact:true}).click();
  await page.getByRole('heading',{name:'运行观测',exact:true}).waitFor();await page.getByRole('heading',{name:'模型调用',exact:true}).waitFor();
  report.stage='refresh';await page.getByRole('button',{name:'刷新记录',exact:true}).click();await page.getByRole('heading',{name:'模型调用',exact:true}).waitFor();
  for(const [width,height] of [[1440,1000],[1024,768],[820,1180],[768,1024],[390,844]]){
    await page.setViewportSize({width,height});await page.waitForTimeout(150);
    report.stage='size-'+width;
    const geometry=await page.evaluate(()=>({page:document.documentElement.scrollWidth,viewport:innerWidth,main:document.querySelector('.desk-main')?.getBoundingClientRect().width}));
    if(geometry.page>geometry.viewport){report.overflow=geometry;await page.screenshot({path:`${output}/overflow-${width}.png`});throw new Error('Page overflow');}
    await page.screenshot({path:`${output}/admin-${width}.png`,fullPage:true});report.sizes.push({width,height,no_page_overflow:true});
  }
  report.passed=true;
} catch(error){report.error_type=error.name;process.exitCode=1;}
finally { if(headers)await context.request.post('http://127.0.0.1:8000/api/auth/logout',{headers}).catch(()=>{});await context.close();await browser.close();await writeFile(`${output}/browser-live.json`,JSON.stringify(report,null,2)+'\n'); }

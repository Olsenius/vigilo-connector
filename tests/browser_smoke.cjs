const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1100,height:950}});
  const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  const base=process.env.SMOKE_BASE;
  await page.goto(base+'/admin');
  await page.locator('input[name="password"]').fill('x'.repeat(48));
  await Promise.all([page.waitForURL(base+'/admin'),page.locator('button').first().click()]);
  await page.goto(base+'/admin/u/vigilo');
  await page.getByRole('button',{name:'Logg inn hos vigilo',exact:true}).click();
  const link=await page.getByRole('link',{name:'Opne innlogging'}).getAttribute('href');
  const state=new URL(link).searchParams.get('state');
  await page.screenshot({path:'/tmp/vigilo-v2-browser-login.png',fullPage:true});
  await page.getByLabel('Returadresse').fill('app://test?'+new URLSearchParams({state,code:'browser-synthetic'}));
  await page.getByRole('button',{name:'Fullfør innlogging',exact:true}).click();
  await page.getByText('Connected.',{exact:true}).waitFor();
  await page.getByText('Status: Tilkopla',{exact:true}).waitFor();
  await page.screenshot({path:'/tmp/vigilo-v2-browser-connected.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('mobile horizontal overflow');
  await page.screenshot({path:'/tmp/vigilo-v2-browser-mobile.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
  console.log('PASS: browser login form, full setup flow, connected status and mobile layout');
 }finally{await browser.close()}
})().catch(error=>{console.error(error);process.exit(1)});

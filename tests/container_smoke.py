"""Opt-in Docker smoke test using synthetic Vigilo responses only.

Build vigilo-mcp:v2-local-review and mcp-gateway:v2-local-review first.
Run with the development virtualenv Python. Requires sudo -n docker access.
Creates and removes its own network and containers; never uses account data.
"""
import html,json,os,pathlib,re,subprocess,tempfile,time,uuid
from urllib.parse import parse_qs,urlsplit,urlencode
import httpx

def docker(*args):
 return subprocess.check_output(['sudo','-n','docker',*args],text=True).strip()
suffix=uuid.uuid4().hex[:10]
network='vigilo-v2-smoke-'+suffix
backend='vigilo-v2-backend-'+suffix
gateway='vigilo-v2-gateway-'+suffix
with tempfile.TemporaryDirectory() as directory:
 script=pathlib.Path(directory)/'backend.py'
 script.write_text('''from vigilo_connector.http_app import Service,create_app
from vigilo_connector import auth
import uvicorn
s=Service()
s.client.children=lambda: []
s.client.get_children=lambda **kwargs: [{"childId":"synthetic-child"}]
auth.authorize_url=lambda state: "https://login.example/authorize?state="+state
auth.exchange_code=lambda code: dict(access_token="synthetic-access",refresh_token="synthetic-refresh",expires_in=3600)
uvicorn.run(create_app("synthetic-internal",s),host="0.0.0.0",port=8080)
''')
 os.chmod(directory,0o755);script.chmod(0o644)
 try:
  docker('network','create',network)
  docker('run','-d','--rm','--name',backend,'--network',network,'--network-alias','vigilo','-e','MCP_AUTH_TOKEN=synthetic-internal','-v',str(script)+':/run/smoke.py:ro','vigilo-mcp:v2-local-review','python','/run/smoke.py')
  docker('run','-d','--rm','--name',gateway,'--network',network,'-p','127.0.0.1::8080','-e','UPSTREAMS=vigilo=http://vigilo:8080','-e','UPSTREAM_TOKEN_VIGILO=synthetic-internal','-e','ADMIN_PASSWORD='+'x'*48,'mcp-gateway:v2-local-review')
  port=json.loads(docker('inspect',gateway))[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
  with httpx.Client(base_url='http://127.0.0.1:'+port,follow_redirects=True,timeout=15) as c:
   for _ in range(60):
    try:
     if c.get('/healthz').status_code==200:break
    except httpx.HTTPError:pass
    time.sleep(.1)
   assert c.post('/vigilo/mcp',json={}).status_code==401
   c.post('/admin/login',data={'password':'x'*48})
   def page():return c.get('/admin/u/vigilo').text
   for _ in range(100):
    markup=page()
    if "Logg inn hos" in markup:break
    time.sleep(.1)
   assert "Logg inn hos" in markup,markup
   csrf=re.search(r'name="csrf" value="([^"]+)"',markup).group(1)
   base={'csrf':csrf}
   c.post('/admin/u/vigilo/save',data=dict(base,f_VIGILO_CLIENT_ID='synthetic-id',f_VIGILO_CLIENT_SECRET='synthetic-secret'))
   markup=c.post('/admin/u/vigilo/setup/start',data=base).text
   assert 'Opne innlogging' in markup,markup
   attempt=re.search(r'name="attempt_id" value="([^"]+)"',markup).group(1)
   link=html.unescape(re.search(r'href="(https://login.example/[^"]+)"',markup).group(1))
   state=parse_qs(urlsplit(link).query)['state'][0]
   markup=c.post('/admin/u/vigilo/setup/complete',data=dict(base,attempt_id=attempt,input='app://synthetic?'+urlencode(dict(state=state,code='synthetic-code')))).text
   assert 'Connected.' in markup,markup
   assert 'Status: Tilkopla' in markup
   markup=c.post('/admin/tokens',data=dict(base,name='synthetic',resource='vigilo')).text
   token=re.search(r'gw_[A-Za-z0-9_-]+',markup).group(0)
   headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}
   def rpc(method,params):
    response=c.post('/vigilo/mcp',headers=headers,json=dict(jsonrpc='2.0',id=1,method=method,params=params))
    assert response.status_code==200,response.text
    if response.headers['content-type'].startswith('application/json'):return response.json()
    return json.loads([line[5:].strip() for line in response.text.splitlines() if line.startswith('data:')][-1])
   rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='smoke',version='1')))
   assert len(rpc('tools/list',{})['result']['tools'])==47
   assert 'synthetic-child' in str(rpc('tools/call',dict(name='list_children',arguments={})))
   if os.environ.get('SMOKE_BROWSER_SCRIPT'):
    subprocess.run(['node', os.environ['SMOKE_BROWSER_SCRIPT']],env=dict(os.environ,SMOKE_BASE='http://127.0.0.1:'+port),check=True)
   print('PASS: container admin/config/login + gateway-authenticated MCP initialize, 47 tools and tool call')
 except BaseException:
  print(docker("logs",backend))
  raise
 finally:
  for name in (gateway,backend):
   subprocess.run(['sudo','-n','docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  subprocess.run(['sudo','-n','docker','network','rm',network],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

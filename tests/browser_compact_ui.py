"""Synthetic high-volume compactness/journey regression; no real mutations."""
import importlib.util,json,os,sys
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
script=Path(sys.argv[1]).resolve()
spec=importlib.util.spec_from_file_location('operator_compact_browser',script)
assert spec is not None and spec.loader is not None
app=importlib.util.module_from_spec(spec);sys.modules[spec.name]=app;spec.loader.exec_module(app)
out=Path(os.environ.get('OPERATOR_BROWSER_OUTPUT','/tmp/operator-compact-browser'));out.mkdir(parents=True,exist_ok=True)
fixture={'checked_at_display':'22.11 15:00 МСК','services':[{'label':f'Сервис {i}','status':'ok' if i else 'limited','caption':'Подключение'}for i in range(12)],'subscriptions':[{'id':f'p{i}','label':f'Провайдер {i}','available_accounts':8,'attention_accounts':1,'accounts':[{'label':f'Аккаунт {j}','status':'ok','plan':'Plus','reset_bank':0,'windows':[{'label':'5 часов','used_percent':35,'remaining_percent':65,'reset_text':'Сброс через 2 часа'}]}for j in range(9)]}for i in range(3)],'server':{'disk':{'status':'ok','used_percent':48,'used_bytes':48*2**30,'available_bytes':52*2**30,'total_bytes':100*2**30}},'vpn':{'enabled':True,'profiles':[{'id':'main','label':'Happ · основной профиль','status':'running'}]},'vpn_can_copy':True,'cron_jobs':[{'id':f'job{i}','name':f'Задача {i} с длинным названием для проверки вложенности','state':'active','schedule_kind':'interval','schedule_display':'каждые 30 минут','next_run_display':'22.11 15:30','schedule_minutes':30,'periodicity_index':5,'reasoning_index':4}for i in range(40)],'cron_can_edit':True,'cron_models':[{'value':'default','label':'По умолчанию'}],'connected_users':[{'id':'test','label':'Друг · Hermes','status':'limited','server':{'gateway':'active','disk_free_pct':76,'mem_available_pct':81},'profile':{'status':'usage_unavailable','reset_bank':0}}]}
mutations=[]
with sync_playwright()as pw:
 b=pw.chromium.launch(headless=True,args=['--no-sandbox'])
 c=b.new_context(viewport={'width':390,'height':844})
 c.add_init_script('window.Telegram={WebApp:{initData:"synthetic-a",ready(){},expand(){}}}')
 c.route('https://telegram.org/**',lambda r:r.fulfill(body=''))
 c.route('https://operator.example.invalid/miniapp',lambda r:r.fulfill(content_type='text/html',body=app.MINIAPP_HTML))
 def api(r):
  data=r.request.post_data_json
  if data.get('action'):mutations.append(data['action']);r.fulfill(status=403,json={'detail':'no test mutations'})
  elif data['init_data']=='invalid':r.fulfill(status=403,json={'detail':'denied'})
  else:r.fulfill(json=fixture)
 c.route('https://operator.example.invalid/api/status**',api)
 p=c.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 p.goto('https://operator.example.invalid/miniapp')
 expect(p.locator('#verified')).to_contain_text('verified')
 roots=p.locator('#content > details.section-fold')
 expect(roots).to_have_count(5)
 expect(p.locator('[data-fold-key="subscriptions"] > .fold-warning')).to_contain_text('требуют внимания: 3')
 assert roots.evaluate_all('els=>els.every(e=>!e.open)')
 for width in(320,390,1280):
  p.set_viewport_size({'width':width,'height':844})
  assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
  assert p.evaluate('document.documentElement.scrollHeight')<=844,'closed overview should fit one viewport'
  p.screenshot(path=str(out/f'closed-{width}.png'))
 p.set_viewport_size({'width':390,'height':844})
 p.locator('[data-fold-key="server"] > summary').focus()
 p.keyboard.press('Enter')
 expect(p.locator('#server-status .bar')).to_be_visible()
 p.locator('[data-fold-key="services"] > summary').click()
 assert not p.locator('[data-fold-key="server"]').evaluate('e=>e.open')
 expect(p.locator('.row').filter(has_text='Сервис 0')).to_be_visible()
 p.locator('[data-fold-key="subscriptions"] > summary').click()
 dashboard=p.locator('.subscription-dashboard')
 assert dashboard.evaluate('e=>e.scrollHeight>e.clientHeight&&e.clientHeight<=480')
 expect(dashboard.locator('[data-micro-account]')).to_have_count(27)
 assert p.locator('[data-fold-key="subscriptions"] details').count()==0
 account=dashboard.locator('[data-micro-account]').first
 expect(account.locator('.micro-bar')).to_be_visible()
 expect(account).to_contain_text('65%')
 expect(account).to_contain_text('Ресеты: 0')
 p.screenshot(path=str(out/'nested-accounts-390.png'))
 p.locator('[data-fold-key="cron"] > summary').click()
 assert p.locator('[data-fold-key="cron"] .compact-list').evaluate('e=>e.scrollHeight>e.clientHeight&&e.clientHeight<=380')
 p.locator('[data-fold-key="cron"] .compact-list').focus();p.keyboard.press('End')
 p.wait_for_function("document.querySelector('[data-fold-key=cron] .compact-list').scrollTop>0")
 assert p.evaluate('document.documentElement.scrollHeight')<1400
 job=p.locator('[data-fold-key="job:job0"]')
 job.locator(':scope > summary').click()
 editor=job.locator('[data-cron-editor]');editor.locator('summary').click()
 field=editor.locator('[data-reasoning]');field.fill('5')
 p.locator('[data-fold-key="services"] > summary').click()
 p.locator('[data-fold-key="cron"] > summary').click()
 expect(field).to_have_value('5')
 p.screenshot(path=str(out/'nested-cron-390.png'))
 assert p.evaluate('load(true)')is True
 expect(p.locator('[data-fold-key="cron"]')).to_have_attribute('open','')
 expect(job).to_have_attribute('open','')
 expect(editor).to_have_attribute('open','')
 # Opening another job collapses the previous one, without mutating records.
 other=p.locator('[data-fold-key="job:job1"]');other.locator(':scope > summary').click()
 expect(job).not_to_have_attribute('open','')
 if p.locator('#connected-tab').count():
  p.locator('#connected-tab').click()
  peer=p.locator('#connected-content > details')
  assert not peer.evaluate('e=>e.open')
  peer.locator(':scope > summary').click()
  expect(peer.locator('.remote-section').first).to_be_visible()
  expect(peer).to_contain_text('Нет данных')
  p.locator('#overview-tab').click()
 assert p.evaluate('load(true)')is True
 expect(other).to_have_attribute('open','')
 # Identity changes must not inherit previous open-state or private contents.
 p.evaluate('window.Telegram.WebApp.initData="synthetic-b"')
 assert p.evaluate('load(true)')is True
 assert roots.evaluate_all('els=>els.every(e=>!e.open)')
 # An unavailable tracker must warn at both closed levels, even with zero accounts.
 fixture['subscriptions']=[{'id':'failed','label':'Трекер','error':'Tracker failed','accounts':[],'attention_accounts':0,'available_accounts':0}]
 assert p.evaluate('load(true)')is True
 failed_root=p.locator('[data-fold-key="subscriptions"]')
 expect(failed_root.locator(':scope > summary.fold-warning')).to_contain_text('Ошибки трекеров: 1')
 assert not failed_root.evaluate('e=>e.open')
 failed_root.locator(':scope > summary').click()
 failed_provider=p.locator('[data-micro-provider="failed"]')
 expect(failed_provider).to_be_visible()
 expect(failed_provider).to_contain_text('Ошибка получения данных')
 expect(failed_provider).to_contain_text('Tracker failed')
 p.evaluate('window.Telegram.WebApp.initData="invalid";load(true)')
 expect(p.locator('#error')).to_be_visible()
 assert p.locator('#content').inner_text()==''
 if p.locator('#connected-content').count():assert p.locator('#connected-content').inner_text()==''
 assert p.evaluate('localStorage.length+sessionStorage.length')==0
 assert not errors and not mutations,(errors,mutations)
 b.close()
print(json.dumps({'result':'passed','fixtures':{'tasks':40,'accounts':27,'services':12},'viewports':[320,390,1280],'mutations':mutations,'screenshots':str(out)}))

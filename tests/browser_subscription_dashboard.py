"""Subscription menu opens every micro dashboard, active first; no real writes."""
import importlib.util,json,os,sys
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
script=Path(sys.argv[1]).resolve();spec=importlib.util.spec_from_file_location('operator_subscription_browser',script)
assert spec and spec.loader
app=importlib.util.module_from_spec(spec);sys.modules[spec.name]=app;spec.loader.exec_module(app)
out=Path(os.environ.get('OPERATOR_BROWSER_OUTPUT','/tmp/operator-subscription-browser'));out.mkdir(parents=True,exist_ok=True)
fixture={'checked_at_display':'22.11 17:00 МСК','services':[],'vpn':{'enabled':False},'server':{'enabled':False},'cron_jobs':[],'subscriptions':[
 {'id':'first','label':'Провайдер A','accounts':[
  {'id':'a','label':'Нужен вход','status':'reauth','message':'Повторите авторизацию','reset_bank':None,'windows':[]},
  {'id':'b','label':'Активный аккаунт с длинным названием и <img src=x onerror=alert(1)>','status':'ok','plan':'Plus','reset_bank':0,'windows':[{'label':'5 часов','remaining_percent':65,'used_percent':35,'reset_text':'Сброс через 2 часа'},{'label':'Неделя','remaining_percent':43,'used_percent':57,'reset_text':'Сброс 24.11 17:00 МСК'}]},
  {'id':'c','label':'Неизвестный лимит','status':'unknown','windows':[{'label':'5 часов','remaining_percent':None,'used_percent':None,'reset_text':None}]}]},
 {'id':'second','label':'Провайдер B','accounts':[
  {'id':'e','label':'Лимит исчерпан','status':'limited','reset_bank':2,'windows':[{'label':'5 часов','remaining_percent':0,'used_percent':100,'reset_text':'Сброс через час'}]},
  {'id':'d','label':'Второй активный','status':'available','plan':'Premium','reset_bank':3,'windows':[{'label':'Неделя','remaining_percent':78,'used_percent':22,'reset_text':'Сброс завтра'}]}]},
 {'id':'failed','label':'Ошибка трекера','error':'Tracker failed','accounts':[],'attention_accounts':0},
 {'id':'empty','label':'Пустой трекер','accounts':[],'attention_accounts':0}]}
expected_accounts=[a for s in fixture['subscriptions']for a in s['accounts']]
mutations=[]
with sync_playwright()as pw:
 b=pw.chromium.launch(headless=True,args=['--no-sandbox']);c=b.new_context(viewport={'width':390,'height':844})
 c.add_init_script('window.Telegram={WebApp:{initData:"synthetic-a",ready(){},expand(){}}}')
 c.route('https://telegram.org/**',lambda r:r.fulfill(body=''))
 c.route('https://operator.example.invalid/miniapp',lambda r:r.fulfill(content_type='text/html',body=app.MINIAPP_HTML))
 def api(r):
  data=r.request.post_data_json
  if data.get('action'):mutations.append(data['action']);r.fulfill(status=403,json={'detail':'no mutation'})
  elif data['init_data']=='invalid':r.fulfill(status=403,json={'detail':'denied'})
  else:r.fulfill(json=fixture)
 c.route('https://operator.example.invalid/api/status**',api)
 p=c.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 p.goto('https://operator.example.invalid/miniapp');expect(p.locator('#verified')).to_contain_text('verified')
 root=p.locator('[data-fold-key="subscriptions"]');root.locator(':scope > summary').click()
 cards=root.locator('.subscription-micro');expect(cards).to_have_count(len(expected_accounts)+2)
 assert root.locator('details').count()==0,'subscriptions must not need further disclosures'
 accounts=root.locator('[data-micro-account]')
 assert accounts.evaluate_all('els=>els.map(e=>e.dataset.microAccount)')[:2]==['b','d']
 assert set(accounts.evaluate_all('els=>els.map(e=>e.dataset.microAccount)'))=={a['id']for a in expected_accounts}
 assert cards.evaluate_all('els=>els.every(e=>getComputedStyle(e).display!=="none")')
 active=root.locator('[data-micro-account="b"]')
 expect(active).to_contain_text('65% осталось');expect(active).to_contain_text('43% осталось')
 expect(active).to_contain_text('Сброс через 2 часа');expect(active).to_contain_text('Сброс 24.11 17:00 МСК')
 expect(active).to_contain_text('Ресеты: 0');assert active.locator('img').count()==0
 exhausted=root.locator('[data-micro-account="e"]');expect(exhausted).to_contain_text('0% осталось');expect(exhausted).to_contain_text('Ресеты: 2')
 unknown=root.locator('[data-micro-account="c"]');expect(unknown).to_contain_text('Нет данных');expect(unknown).not_to_contain_text('0%')
 expect(root.locator('[data-micro-provider="failed"]')).to_contain_text('Tracker failed')
 expect(root.locator('[data-micro-provider="empty"]')).to_contain_text('Нет данных')
 for width in(320,390,1280):
  p.set_viewport_size({'width':width,'height':844});assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
  listing=root.locator('.subscription-dashboard');assert listing.evaluate('e=>e.scrollHeight>e.clientHeight&&e.clientHeight<=480')
  listing.evaluate('e=>e.scrollTop=0');p.screenshot(path=str(out/f'subscriptions-{width}.png'))
  assert active.locator('.micro-window').evaluate_all('els=>els.every(e=>e.getBoundingClientRect().width>90)')
 assert p.evaluate('load(true)')is True;expect(root).to_have_attribute('open','');expect(cards).to_have_count(len(expected_accounts)+2)
 fixture['subscriptions'][0]['accounts'][1]['status']='reauth'
 assert p.evaluate('load(true)')is True
 assert accounts.first.get_attribute('data-micro-account')=='d','refresh must re-sort current state'
 p.evaluate('window.Telegram.WebApp.initData="invalid";load(true)');expect(p.locator('#error')).to_be_visible()
 assert p.locator('.subscription-micro').count()==0 and p.locator('#content').inner_text()==''
 assert not errors and not mutations,(errors,mutations);assert p.evaluate('localStorage.length+sessionStorage.length')==0
 b.close()
print(json.dumps({'result':'passed','accounts':len(expected_accounts),'provider_errors':1,'empty_providers':1,'viewports':[320,390,1280],'mutations':mutations,'screenshots':str(out)}))

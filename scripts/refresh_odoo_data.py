"""
ELMI Power Dashboard – Odoo Data Refresh
Fetches fresh leads, activities and lead-meta from Odoo,
then injects them directly into sales.html.
Reads credentials from environment variables (GitHub Secrets).
"""
import os, re, json, sys
from xmlrpc import client as xc
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding='utf-8')

# ── Credentials from env (GitHub Secrets) ──────────────────────────────────
URL  = os.environ.get('ODOO_URL',  'https://elmi-power-gmbh1.odoo.com')
DB   = os.environ.get('ODOO_DB',   'elmi-power-gmbh1')
USER = os.environ.get('ODOO_USER', 'wangyu.chen@elmipower.de')
KEY  = os.environ.get('ODOO_KEY')
if not KEY:
    print('ERROR: ODOO_KEY environment variable not set', flush=True)
    sys.exit(1)

HTML_PATH = os.environ.get('HTML_PATH', 'sales.html')

# ── Connect ─────────────────────────────────────────────────────────────────
print('Connecting to Odoo...', flush=True)
common = xc.ServerProxy(f'{URL}/xmlrpc/2/common')
uid    = common.authenticate(DB, USER, KEY, {})
rpc    = xc.ServerProxy(f'{URL}/xmlrpc/2/object')
print(f'Authenticated as UID {uid}', flush=True)

# ── Existing group assignments (keep if already classified) ─────────────────
with open(HTML_PATH, encoding='utf-8') as f:
    sales_html = f.read()

m_old = re.search(r'const ALL_LEADS = (\[.*?\]);', sales_html, re.DOTALL)
old_groups = {}
if m_old:
    try:
        old_leads = json.loads(m_old.group(1))
        old_groups = {l['id']: l.get('group', 'Other') for l in old_leads}
        print(f'Loaded {len(old_groups)} existing group assignments', flush=True)
    except Exception as e:
        print(f'Warning: could not parse old leads: {e}', flush=True)

# ── Tag → group mapping ──────────────────────────────────────────────────────
TAG_GROUP = {
    34: 'CPO', 30: 'CPO', 31: 'CPO', 32: 'CPO', 33: 'CPO',
    35: 'EPC',
    19: 'Wholesale',
    10: 'Logistik',
}

def assign_group(lead_id, tags, name, partner):
    if lead_id in old_groups:
        return old_groups[lead_id]
    for t in tags:
        if t in TAG_GROUP:
            return TAG_GROUP[t]
    c = (name + ' ' + (partner or '')).lower()
    if any(w in c for w in ['utility','stadtwerk','ewv','e.on','enervie','ew ','energie werk']): return 'Utility'
    if any(w in c for w in ['wholesale','großhand','distribut']): return 'Wholesale'
    if any(w in c for w in ['logistik','spedition','transport','flotte']): return 'Logistik'
    if any(w in c for w in ['epc','contractor','solar','wind']): return 'EPC'
    if any(w in c for w in ['cpo','charging','ladesäule','ladestation']): return 'CPO'
    return 'Other'

# ── 1. Fetch Leads ───────────────────────────────────────────────────────────
print('Fetching leads...', flush=True)
raw_leads = rpc.execute_kw(DB, uid, KEY, 'crm.lead', 'search_read',
    [[['active','in',[True,False]]]],
    {'fields': ['id','name','partner_id','user_id','stage_id',
                'expected_revenue','probability','date_deadline','tag_ids'],
     'limit': 1000})

leads_out = []
for l in raw_leads:
    partner = l['partner_id'][1] if l['partner_id'] else ''
    rep     = l['user_id'][1]    if l['user_id']    else ''
    stage   = l['stage_id'][1]   if l['stage_id']   else ''
    grp     = assign_group(l['id'], l['tag_ids'], l['name'], partner)
    leads_out.append({
        'id':       l['id'],
        'name':     l['name'][:80],
        'partner':  partner[:60],
        'revenue':  round(l['expected_revenue'] or 0, 2),
        'prob':     round(l['probability'] or 0, 1),
        'stage':    stage,
        'rep':      rep,
        'deadline': l['date_deadline'] or '',
        'group':    grp,
    })

reps = Counter(l['rep'] for l in leads_out)
print(f'Leads: {len(leads_out)} total. Reps: {dict(sorted(reps.items(), key=lambda x:-x[1]))}', flush=True)

# ── 2. Fetch Lead Meta (create date, medium) ─────────────────────────────────
print('Fetching lead meta...', flush=True)
raw_meta = rpc.execute_kw(DB, uid, KEY, 'crm.lead', 'search_read',
    [[['active','in',[True,False]]]],
    {'fields': ['id','create_date','medium_id'], 'limit': 1000})

meta_out = [{'id': l['id'],
              'cr': l['create_date'][:10] if l['create_date'] else '',
              'med': l['medium_id'][1] if l['medium_id'] else ''}
            for l in raw_meta]
print(f'Meta: {len(meta_out)} entries', flush=True)

# ── 3. Fetch Activities (messages on CRM leads) ───────────────────────────────
INTERNAL = {'Jonas Meyer','Stefan Hahn','Christian Elverfeld','Wangyu Chen',
            'Benjamin Strieder','Mick Meyer','Kalender Dirik','Koray Erün','Matthias Schmidt'}

def classify_dir(author, msg_type):
    if author not in INTERNAL and not any(i in author for i in INTERNAL):
        return 'Ex-In'
    return 'In-Ex' if msg_type == 'email' else 'In-In'

print('Fetching sale orders for ho field + YTD revenue...', flush=True)
orders = rpc.execute_kw(DB, uid, KEY, 'sale.order', 'search_read',
    [[['state','in',['sale','done']]]],
    {'fields': ['opportunity_id','date_order','amount_untaxed','name','partner_id'], 'limit': 2000})
opp_ids_with_order = {o['opportunity_id'][0] for o in orders if o.get('opportunity_id')}

# ── YTD revenue from sale.order (Nettobetrag) ────────────────────────────────
from collections import defaultdict
ytd_by_month = defaultdict(float)
ytd_orders_out = []
for o in orders:
    d = (o.get('date_order') or '')[:10]
    if not d.startswith('2026-'):
        continue
    mo = d[:7]
    net = round(o.get('amount_untaxed') or 0, 2)
    ytd_by_month[mo] += net
    ytd_orders_out.append({
        'id':      o['id'],
        'name':    o.get('name',''),
        'partner': o['partner_id'][1] if o.get('partner_id') else '',
        'date':    d,
        'mo':      mo,
        'net':     net,
    })

MONTH_LABELS = {
    '2026-01':'Januar 2026','2026-02':'Februar 2026','2026-03':'März 2026',
    '2026-04':'April 2026','2026-05':'Mai 2026','2026-06':'Juni 2026',
    '2026-07':'Juli 2026','2026-08':'August 2026','2026-09':'September 2026',
    '2026-10':'Oktober 2026','2026-11':'November 2026','2026-12':'Dezember 2026',
}
ytd_months_out = [
    {'mo': mo, 'label': MONTH_LABELS.get(mo, mo), 'net': round(v, 2)}
    for mo, v in sorted(ytd_by_month.items())
]
print(f'YTD 2026 sale orders: {len(ytd_orders_out)}, total net: {sum(v["net"] for v in ytd_months_out):,.0f} EUR', flush=True)

print('Fetching messages...', flush=True)
msgs = rpc.execute_kw(DB, uid, KEY, 'mail.message', 'search_read',
    [[['model','=','crm.lead'],
      ['message_type','in',['email','comment']],
      ['date','>=','2025-01-01'],
      ['author_id','!=',False]]],
    {'fields': ['id','res_id','author_id','date','message_type',
                'mail_activity_type_id'], 'limit': 5000})

print(f'Messages: {len(msgs)}', flush=True)

lead_ids_set = {l['id'] for l in leads_out}
acts_out = []
for msg in msgs:
    lid = msg.get('res_id', 0)
    if lid not in lead_ids_set:
        continue
    atype  = msg.get('mail_activity_type_id')
    mtype  = msg.get('message_type','')
    author = msg['author_id'][1] if msg.get('author_id') else 'Unbekannt'
    t      = atype[1] if atype else ('Email' if mtype=='email' else 'Notiz')
    tl     = t.lower()
    cat    = 'Email' if ('email' in tl or 'mail' in tl) else 'Anruf' if ('call' in tl or 'anruf' in tl) else 'Sonstiges'
    d      = msg['date'][:10]
    acts_out.append({
        'id':  msg['id'],
        'lid': lid,
        'u':   author,
        'ty':  t,
        'ca':  cat,
        'd':   d,
        'mo':  d[:7],
        'st':  'erledigt',
        'ho':  1 if lid in opp_ids_with_order else 0,
        'sg':  '',
        'sgn': -1,
        'pt':  '',
        'lr':  '',
        'dir': classify_dir(author, mtype),
    })

dirs = Counter(a['dir'] for a in acts_out)
print(f'Activities: {len(acts_out)}. Directions: {dict(dirs)}', flush=True)

# ── 4. Inject into sales.html ─────────────────────────────────────────────────
print('Injecting data into sales.html...', flush=True)

new_leads_js  = 'const ALL_LEADS = '    + json.dumps(leads_out,       ensure_ascii=False, separators=(',',':')) + ';'
new_acts_js   = 'const ALL_ACTS2 = '   + json.dumps(acts_out,        ensure_ascii=True,  separators=(',',':')) + ';'
new_meta_js   = 'const LEAD_META = '   + json.dumps(meta_out,        ensure_ascii=False, separators=(',',':')) + ';'
new_ytd_js    = 'const INVOICE_YTD = ' + json.dumps({'months': ytd_months_out, 'orders': ytd_orders_out}, ensure_ascii=False, separators=(',',':')) + ';'

def safe_replace(html, pattern, replacement, label):
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        print(f'WARNING: {label} pattern not found in HTML', flush=True)
        return html
    return html[:m.start()] + replacement + html[m.end():]

sales_html = safe_replace(sales_html, r'const ALL_LEADS = \[.*?\];',    new_leads_js,  'ALL_LEADS')
sales_html = safe_replace(sales_html, r'const ALL_ACTS2 = \[.*?\];',    new_acts_js,   'ALL_ACTS2')
sales_html = safe_replace(sales_html, r'const LEAD_META = \[.*?\];',    new_meta_js,   'LEAD_META')
sales_html = safe_replace(sales_html, r'const INVOICE_YTD = \{.*?\};',  new_ytd_js,    'INVOICE_YTD')

with open(HTML_PATH, 'w', encoding='utf-8') as f:
    f.write(sales_html)

print(f'Done. sales.html updated: {len(sales_html):,} chars', flush=True)

# ── Summary ───────────────────────────────────────────────────────────────────
groups = Counter(l['group'] for l in leads_out)
print('\n── Summary ──────────────────────────────', flush=True)
print(f'Leads:      {len(leads_out)}', flush=True)
print(f'Activities: {len(acts_out)}', flush=True)
print(f'Groups:     {dict(sorted(groups.items(), key=lambda x:-x[1]))}', flush=True)
print('Rep counts:', dict(sorted(reps.items(), key=lambda x:-x[1])), flush=True)

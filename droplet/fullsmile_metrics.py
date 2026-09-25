#!/usr/bin/env python3
"""
Full Smile Dental: practice metrics for the dashboard.

Runs on the DigitalOcean droplet that already syncs OpenDental <-> GHL.

  * Reads OpenDental through the Open Dental API (read-only SQL: PUT /queries/ShortQuery).
  * Reads GHL (contacts, opportunities, pipelines).
  * Matches new patients to the GHL lead they came from, ON THIS MACHINE.
  * Writes ONLY totals: counts and dollar amounts per day or per week, per channel.
    No names, phone numbers, emails, PatNums or individual visit dates leave the droplet.
  * Commits that file (data/practice.json) to the private dashboard repo on GitHub,
    which rebuilds the dashboard.

Usage
  python3 fullsmile_metrics.py            build and push
  python3 fullsmile_metrics.py --dry-run  build, write ./practice.json, do not push
  python3 fullsmile_metrics.py --check    test OpenDental, GHL and GitHub access, then stop
  python3 fullsmile_metrics.py --audit --channel=lsa   list each new patient credited to a channel (screen only)

Settings come from environment variables, or from a file named fullsmile_metrics.env
next to this script (KEY=value per line). See .env.example.
Standard library only; no pip installs.
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# GHL is behind Cloudflare, which rejects Python's default User-Agent (error 1010).
_opener = urllib.request.build_opener()
_opener.addheaders = [('User-Agent', 'Mozilla/5.0 (fullsmile-metrics)')]
urllib.request.install_opener(_opener)


def load_env_file():
    path = os.environ.get('FS_ENV_FILE') or os.path.join(HERE, 'fullsmile_metrics.env')
    if not os.path.exists(path):
        return
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file()
E = os.environ.get

OD_BASE = E('OD_API_BASE', 'https://api.opendental.com/api/v1').rstrip('/')
OD_AUTH = E('OD_AUTH') or f"ODFHIR {E('OD_DEVELOPER_KEY', '')}/{E('OD_CUSTOMER_KEY', '')}"
GHL_BASE = 'https://services.leadconnectorhq.com'
GHL_TOKEN = E('GHL_TOKEN', '')
GHL_LOC = E('GHL_LOCATION_ID', '')
GH_TOKEN = E('GITHUB_TOKEN', '')
GH_REPO = E('GITHUB_REPO', 'flow-co-ai/full-smile')
GH_PATH = E('GITHUB_PATH', 'data/practice.json')
GH_BRANCH = E('GITHUB_BRANCH', 'main')
# The GHL contact custom field that holds the OpenDental PatNum (id or key). Optional: phone/email matching is the fallback.
PATNUM_FIELD = E('GHL_PATNUM_FIELD', '')
# A GHL custom field with the service the lead asked about (e.g. "Service", "Service Interested In"). Optional.
SERVICE_FIELD = E('GHL_SERVICE_FIELD', 'service')
DAYS_BACK = int(E('FS_DAYS_BACK', '400'))
DAYS_AHEAD = int(E('FS_DAYS_AHEAD', '35'))

NOTES = []


# ---------------------------------------------------------------- http
def http(method, url, headers=None, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read().decode('utf-8') or 'null'
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            msg = e.read().decode('utf-8', 'replace')[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f'{method} {url.split("?")[0]} -> {e.code}: {msg}')
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f'{method} {url.split("?")[0]} -> {e}')


# ---------------------------------------------------------------- OpenDental
def od_query(sql):
    """Read-only SQL through the Open Dental API. ShortQuery returns 100 rows per call; page with Offset."""
    rows, offset = [], 0
    while True:
        url = f'{OD_BASE}/queries/ShortQuery' + (f'?Offset={offset}' if offset else '')
        page = http('PUT', url, {'Authorization': OD_AUTH, 'Content-Type': 'application/json'}, {'SqlCommand': sql})
        if isinstance(page, dict):
            page = page.get('data') or page.get('rows') or []
        rows.extend(page or [])
        if not page or len(page) < 100:
            break
        offset += 100
        if offset >= 30000:
            NOTES.append('An OpenDental query hit the 30,000 row safety cap.')
            break
        time.sleep(0.25)
    return rows


def od_try(label, *sqls):
    """Runs the first query that works (older Open Dental versions name some columns differently)."""
    last = None
    for sql in sqls:
        try:
            return od_query(sql)
        except RuntimeError as e:
            last = e
    NOTES.append(f'OpenDental {label} could not be read ({str(last)[:120]}).')
    return []


def d10(v):
    s = str(v or '')[:10]
    return s if re.match(r'^\d{4}-\d{2}-\d{2}$', s) and not s.startswith('0001') else ''


def num(v):
    try:
        return float(str(v).replace(',', '').replace('$', '') or 0)
    except ValueError:
        return 0.0


def monday(d):
    x = dt.date.fromisoformat(d)
    return (x - dt.timedelta(days=x.weekday())).isoformat()


def digits10(v):
    s = re.sub(r'\D', '', str(v or ''))
    return s[-10:] if len(s) >= 10 else ''


# Service category from the CDT code. Standard across practices, unlike each office's own category list.
CAT_SQL = """CASE
  WHEN pc.ProcCode REGEXP '^D994[7-9]' THEN 'sleep'
  WHEN pc.ProcCode LIKE 'D0%%' OR pc.ProcCode LIKE 'D1%%' THEN 'prev'
  WHEN pc.ProcCode LIKE 'D4%%' THEN 'perio'
  WHEN pc.ProcCode REGEXP '^D2[1-6]' THEN 'fill'
  WHEN pc.ProcCode LIKE 'D27%%' OR pc.ProcCode REGEXP '^D6[2-7]' THEN 'crown'
  WHEN pc.ProcCode LIKE 'D3%%' THEN 'endo'
  WHEN pc.ProcCode REGEXP '^D6[01]' THEN 'implant'
  WHEN pc.ProcCode LIKE 'D5%%' THEN 'denture'
  WHEN pc.ProcCode LIKE 'D7%%' THEN 'surgery'
  WHEN pc.ProcCode LIKE 'D8%%' THEN 'ortho'
  ELSE 'other' END""".replace('%%', '%')

PROD = 'pl.ProcFee*(pl.UnitQty+pl.BaseUnits)'


def opendental(a, b, ahead):
    out = {}
    out['production'] = od_query(f"""SELECT pl.ProcDate AS d, ROUND(SUM({PROD}),2) AS amt FROM procedurelog pl
        WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{a}' AND '{b}' GROUP BY pl.ProcDate""")
    out['collPat'] = od_query(f"""SELECT ps.DatePay AS d, ROUND(SUM(ps.SplitAmt),2) AS amt FROM paysplit ps
        WHERE ps.DatePay BETWEEN '{a}' AND '{b}' GROUP BY ps.DatePay""")
    out['collIns'] = od_query(f"""SELECT cp.DateCP AS d, ROUND(SUM(cp.InsPayAmt),2) AS amt FROM claimproc cp
        WHERE cp.Status IN (1,4) AND cp.DateCP BETWEEN '{a}' AND '{b}' GROUP BY cp.DateCP""")
    out['appts'] = od_query(f"""SELECT DATE(ap.AptDateTime) AS d, SUM(ap.AptStatus=2) AS done, SUM(ap.AptStatus=5) AS broken,
        SUM(ap.AptStatus=1) AS sched, SUM(ap.AptStatus=2 AND ap.IsNewPatient=1) AS newdone,
        SUM(ap.AptStatus IN (1,2,5) AND ap.IsNewPatient=1) AS newbooked
        FROM appointment ap WHERE ap.AptDateTime BETWEEN '{a} 00:00:00' AND '{ahead} 23:59:59' GROUP BY DATE(ap.AptDateTime)""")
    out['prodCat'] = od_query(f"""SELECT DATE_SUB(pl.ProcDate, INTERVAL WEEKDAY(pl.ProcDate) DAY) AS wk, {CAT_SQL} AS cat,
        ROUND(SUM({PROD}),2) AS amt, COUNT(*) AS n FROM procedurelog pl JOIN procedurecode pc ON pc.CodeNum=pl.CodeNum
        WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{a}' AND '{b}' GROUP BY wk, cat""")
    out['prodProv'] = od_query(f"""SELECT DATE_SUB(pl.ProcDate, INTERVAL WEEKDAY(pl.ProcDate) DAY) AS wk, pl.ProvNum AS prov,
        ROUND(SUM({PROD}),2) AS amt FROM procedurelog pl
        WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{a}' AND '{b}' GROUP BY wk, prov""")
    out['providers'] = od_try('providers', "SELECT ProvNum AS prov, Abbr AS abbr, FName AS f, LName AS l, IsSecondary AS hyg FROM provider",
                                "SELECT ProvNum AS prov, Abbr AS abbr, FName AS f, LName AS l FROM provider")
    # Snapshots: counts only.
    out['active'] = od_try('active patients', """SELECT COUNT(DISTINCT pl.PatNum) AS n FROM procedurelog pl
        WHERE pl.ProcStatus=2 AND pl.ProcDate >= CURDATE() - INTERVAL 18 MONTH""")
    out['recall'] = od_try('recall', """SELECT SUM(rc.DateDue < CURDATE()) AS overdue,
        SUM(rc.DateDue BETWEEN CURDATE() AND CURDATE() + INTERVAL 30 DAY) AS due30
        FROM recall rc JOIN patient p ON p.PatNum=rc.PatNum
        WHERE rc.IsDisabled=0 AND rc.DateDue >= CURDATE() - INTERVAL 18 MONTH AND rc.DateScheduled < '1880-01-01' AND p.PatStatus=0""")
    out['unsched'] = od_try('unscheduled treatment', f"""SELECT COUNT(DISTINCT pl.PatNum) AS pats, ROUND(SUM({PROD}),2) AS amt
        FROM procedurelog pl JOIN patient p ON p.PatNum=pl.PatNum
        WHERE pl.ProcStatus=1 AND pl.AptNum=0 AND p.PatStatus=0 AND pl.DateTP >= CURDATE() - INTERVAL 18 MONTH""")
    out['brokenOpen'] = od_try('broken appointments', """SELECT COUNT(*) AS n FROM appointment a
        WHERE a.AptStatus=5 AND a.AptDateTime >= CURDATE() - INTERVAL 60 DAY
        AND NOT EXISTS (SELECT 1 FROM appointment b WHERE b.PatNum=a.PatNum AND b.AptStatus IN (1,2) AND b.AptDateTime > a.AptDateTime)""")
    # --- Growth, productivity, treatment, hygiene, schedule and money owed. Every row is a total.
    out.update(practice_depth(a, b, ahead))
    # Patient-level rows for matching. These stay in memory on this machine and are never written out.
    # New patient = first completed visit flagged "new patient" by OpenDental. DateFirstVisit is not used: records created by
    # the GHL sync get it stamped, which counted leads who never came in.
    out['newPatients'] = od_try('new patients', f"""SELECT p.PatNum AS pat, MIN(DATE(ap.AptDateTime)) AS fv, p.WirelessPhone AS m, p.HmPhone AS h,
        p.WkPhone AS w, p.Email AS e FROM appointment ap JOIN patient p ON p.PatNum=ap.PatNum
        WHERE ap.AptStatus=2 AND ap.IsNewPatient=1 AND ap.AptDateTime BETWEEN '{a} 00:00:00' AND '{b} 23:59:59' AND p.PatStatus <> 4
        GROUP BY p.PatNum, p.WirelessPhone, p.HmPhone, p.WkPhone, p.Email""")
    out['newAppts'] = od_query(f"""SELECT ap.PatNum AS pat, DATE(ap.AptDateTime) AS d, ap.AptStatus AS st, p.WirelessPhone AS m,
        p.HmPhone AS h, p.WkPhone AS w, p.Email AS e FROM appointment ap JOIN patient p ON p.PatNum=ap.PatNum
        WHERE ap.IsNewPatient=1 AND ap.AptStatus IN (1,2,5) AND ap.AptDateTime BETWEEN '{a} 00:00:00' AND '{ahead} 23:59:59'""")
    pats = sorted({int(r['pat']) for r in out['newPatients'] if str(r.get('pat', '')).isdigit()})
    out['ref'], out['npColl'], out['npProd'], out['npPlan'], out['npVisits'] = [], [], [], [], []
    for i in range(0, len(pats), 150):
        ids = ','.join(map(str, pats[i:i + 150]))
        out['ref'] += od_try('referral sources',
                             f"""SELECT ra.PatNum AS pat, r.LName AS l, r.FName AS f, r.IsDoctor AS doc, r.NotPerson AS np FROM refattach ra
                             JOIN referral r ON r.ReferralNum=ra.ReferralNum WHERE ra.RefType=1 AND ra.PatNum IN ({ids}) ORDER BY ra.PatNum, ra.ItemOrder""",
                             f"""SELECT ra.PatNum AS pat, r.LName AS l, r.FName AS f, r.IsDoctor AS doc, r.NotPerson AS np FROM refattach ra
                             JOIN referral r ON r.ReferralNum=ra.ReferralNum WHERE ra.IsFrom=1 AND ra.PatNum IN ({ids}) ORDER BY ra.PatNum, ra.ItemOrder""")
        out['npColl'] += od_query(f"""SELECT x.pat, ROUND(SUM(x.amt),2) AS amt FROM (
            SELECT ps.PatNum AS pat, ps.SplitAmt AS amt FROM paysplit ps WHERE ps.PatNum IN ({ids})
            UNION ALL SELECT cp.PatNum, cp.InsPayAmt FROM claimproc cp WHERE cp.Status IN (1,4) AND cp.PatNum IN ({ids})) x GROUP BY x.pat""")
        out['npProd'] += od_query(f"""SELECT pl.PatNum AS pat, ROUND(SUM({PROD}),2) AS amt FROM procedurelog pl
            WHERE pl.ProcStatus=2 AND pl.PatNum IN ({ids}) GROUP BY pl.PatNum""")
        out['npPlan'] += od_query(f"""SELECT pl.PatNum AS pat, ROUND(SUM({PROD}),2) AS amt FROM procedurelog pl
            WHERE pl.ProcStatus=1 AND pl.PatNum IN ({ids}) GROUP BY pl.PatNum""")
        out['npVisits'] += od_try('new patient return visits', f"""SELECT ap.PatNum AS pat, COUNT(*) AS n FROM appointment ap
            WHERE ap.AptStatus=2 AND ap.PatNum IN ({ids}) GROUP BY ap.PatNum""")
    return out


HYG_CODES = "'D1110','D1120','D4910','D4346','D4341','D4342','D4355'"


def practice_depth(a, b, ahead):
    """Totals behind the Growth, Production, Treatment and Hygiene views. No row identifies a patient."""
    o = {}
    wk = lambda col: f"DATE_SUB(DATE({col}), INTERVAL WEEKDAY({col}) DAY)"
    o['codes'] = od_try('procedures by code', f"""SELECT DATE_FORMAT(pl.ProcDate,'%Y-%m') AS m, pc.ProcCode AS code, MAX(pc.Descript) AS d,
        COUNT(*) AS n, ROUND(SUM({PROD}),2) AS amt FROM procedurelog pl JOIN procedurecode pc ON pc.CodeNum=pl.CodeNum
        WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{a}' AND '{b}' GROUP BY m, code""")
    o['provDays'] = od_try('provider days', f"""SELECT DATE_FORMAT(pl.ProcDate,'%Y-%m') AS m, pl.ProvNum AS prov, ROUND(SUM({PROD}),2) AS amt,
        COUNT(DISTINCT pl.ProcDate) AS days, COUNT(DISTINCT pl.PatNum) AS pats FROM procedurelog pl
        WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{a}' AND '{b}' GROUP BY m, prov""")
    # Treatment planned in a month, and where it stands now. Same-day work (never planned ahead) is left out.
    o['tx'] = od_try('treatment plans', f"""SELECT DATE_FORMAT(pl.DateTP,'%Y-%m') AS m, {CAT_SQL} AS cat,
        ROUND(SUM(CASE WHEN pl.ProcStatus=2 THEN {PROD} ELSE 0 END),2) AS done,
        ROUND(SUM(CASE WHEN pl.ProcStatus=1 AND pl.AptNum>0 THEN {PROD} ELSE 0 END),2) AS sched,
        ROUND(SUM(CASE WHEN pl.ProcStatus=1 AND pl.AptNum=0 THEN {PROD} ELSE 0 END),2) AS open,
        COUNT(DISTINCT pl.PatNum) AS pats
        FROM procedurelog pl JOIN procedurecode pc ON pc.CodeNum=pl.CodeNum JOIN patient p ON p.PatNum=pl.PatNum
        WHERE pl.DateTP BETWEEN '{a}' AND '{b}' AND p.PatStatus=0
        AND (pl.ProcStatus=1 OR (pl.ProcStatus=2 AND pl.ProcDate > pl.DateTP)) GROUP BY m, cat""")
    o['unschedAge'] = od_try('unscheduled treatment by age', f"""SELECT CASE WHEN pl.DateTP >= CURDATE() - INTERVAL 30 DAY THEN 'a30'
        WHEN pl.DateTP >= CURDATE() - INTERVAL 90 DAY THEN 'a90' WHEN pl.DateTP >= CURDATE() - INTERVAL 180 DAY THEN 'a180'
        WHEN pl.DateTP >= CURDATE() - INTERVAL 365 DAY THEN 'a365' ELSE 'old' END AS age, {CAT_SQL} AS cat,
        ROUND(SUM({PROD}),2) AS amt, COUNT(DISTINCT pl.PatNum) AS pats
        FROM procedurelog pl JOIN procedurecode pc ON pc.CodeNum=pl.CodeNum JOIN patient p ON p.PatNum=pl.PatNum
        WHERE pl.ProcStatus=1 AND pl.AptNum=0 AND p.PatStatus=0 AND pl.DateTP >= CURDATE() - INTERVAL 18 MONTH GROUP BY age, cat""")
    o['ar'] = od_try('patient balances', """SELECT ROUND(SUM(Bal_0_30),2) AS b0, ROUND(SUM(Bal_31_60),2) AS b30, ROUND(SUM(Bal_61_90),2) AS b60,
        ROUND(SUM(BalOver90),2) AS b90, ROUND(SUM(InsEst),2) AS ins, ROUND(SUM(BalTotal),2) AS tot, SUM(BalTotal > 0.5) AS fams
        FROM patient WHERE PatNum=Guarantor""")
    o['claims'] = od_try('insurance claims', """SELECT CASE WHEN cl.ClaimStatus IN ('U','W','H') THEN 'unsent'
        WHEN cl.DateSent >= CURDATE() - INTERVAL 30 DAY THEN 'd30' WHEN cl.DateSent >= CURDATE() - INTERVAL 60 DAY THEN 'd60'
        WHEN cl.DateSent >= CURDATE() - INTERVAL 90 DAY THEN 'd90' ELSE 'old' END AS b, COUNT(*) AS n,
        ROUND(SUM(cl.ClaimFee),2) AS fee, ROUND(SUM(cl.InsPayEst),2) AS est
        FROM claim cl WHERE cl.ClaimStatus IN ('U','W','H','S') AND cl.ClaimType IN ('P','S','Other')
        AND cl.DateService >= CURDATE() - INTERVAL 24 MONTH GROUP BY b""")
    o['carriers'] = od_try('insurance plans', f"""SELECT DATE_FORMAT(cp.DateCP,'%Y-%m') AS m, c.CarrierName AS car, ROUND(SUM(cp.FeeBilled),2) AS billed,
        ROUND(SUM(cp.InsPayAmt),2) AS paid, ROUND(SUM(cp.WriteOff),2) AS wo, COUNT(*) AS n
        FROM claimproc cp JOIN insplan ip ON ip.PlanNum=cp.PlanNum JOIN carrier c ON c.CarrierNum=ip.CarrierNum
        WHERE cp.Status IN (1,4) AND cp.DateCP BETWEEN '{a}' AND '{b}' GROUP BY m, car""")
    # Hygiene visits, and how many of those patients have a later visit booked or done.
    o['hygiene'] = od_try('hygiene rebooking', f"""SELECT {wk('a.AptDateTime')} AS wk, COUNT(*) AS n,
        SUM(EXISTS(SELECT 1 FROM appointment b WHERE b.PatNum=a.PatNum AND b.AptDateTime > a.AptDateTime AND b.AptStatus IN (1,2))) AS rebooked
        FROM appointment a WHERE a.AptStatus=2 AND a.AptDateTime BETWEEN '{a} 00:00:00' AND '{b} 23:59:59'
        AND EXISTS (SELECT 1 FROM procedurelog pl JOIN procedurecode pc ON pc.CodeNum=pl.CodeNum
                    WHERE pl.AptNum=a.AptNum AND pc.ProcCode IN ({HYG_CODES})) GROUP BY wk""")
    # Chair time: provider schedule hours, and hours booked (Pattern is stored in 5-minute steps).
    o['sched'] = od_try('provider schedules', f"""SELECT {wk('s.SchedDate')} AS wk, s.ProvNum AS prov,
        ROUND(SUM(TIME_TO_SEC(TIMEDIFF(s.StopTime, s.StartTime)))/3600,1) AS hrs FROM schedule s
        WHERE s.SchedType=1 AND s.Status=0 AND s.StopTime > s.StartTime AND s.SchedDate BETWEEN '{a}' AND '{ahead}' GROUP BY wk, prov""",
        f"""SELECT {wk('s.SchedDate')} AS wk, s.ProvNum AS prov,
        ROUND(SUM(TIME_TO_SEC(TIMEDIFF(s.StopTime, s.StartTime)))/3600,1) AS hrs FROM schedule s
        WHERE s.SchedType=1 AND s.StopTime > s.StartTime AND s.SchedDate BETWEEN '{a}' AND '{ahead}' GROUP BY wk, prov""")
    o['booked'] = od_try('booked chair time', f"""SELECT {wk('ap.AptDateTime')} AS wk,
        CASE WHEN ap.IsHygiene=1 AND ap.ProvHyg>0 THEN ap.ProvHyg ELSE ap.ProvNum END AS prov,
        ROUND(SUM(CASE WHEN ap.AptStatus IN (1,2) THEN LENGTH(ap.Pattern) ELSE 0 END)*5/60,1) AS hrs,
        SUM(ap.AptStatus=2) AS done, SUM(ap.AptStatus=1) AS sched, SUM(ap.AptStatus=5) AS broken
        FROM appointment ap WHERE ap.AptStatus IN (1,2,5) AND ap.AptDateTime BETWEEN '{a} 00:00:00' AND '{ahead} 23:59:59' GROUP BY wk, prov""")
    # Active patients (a completed visit in the prior 18 months) at each month end.
    o['activeMonthly'] = []
    first = dt.date.fromisoformat(a).replace(day=1)
    ends, m = [], first
    while m <= dt.date.fromisoformat(b):
        nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        ends.append(min(nxt - dt.timedelta(days=1), dt.date.fromisoformat(b)))
        m = nxt
    for e in ends:
        r = od_try(f'active patients {e:%b %Y}', f"""SELECT COUNT(DISTINCT pl.PatNum) AS n FROM procedurelog pl
            WHERE pl.ProcStatus=2 AND pl.ProcDate BETWEEN '{e}' - INTERVAL 18 MONTH AND '{e}'""")
        if r:
            o['activeMonthly'].append([e.isoformat(), int(num(r[0].get('n')))])
    return o


# ---------------------------------------------------------------- GHL
def ghl_get(url):
    return http('GET', url, {'Authorization': f'Bearer {GHL_TOKEN}', 'Version': '2021-07-28', 'Accept': 'application/json'})


def ghl_pages(url, key, cap=30000):
    out, seen, nxt = [], set(), url
    while nxt and nxt not in seen and len(out) < cap:
        seen.add(nxt)
        j = ghl_get(nxt) or {}
        page = j.get(key) or []
        out += page
        if not page:
            break
        nxt = (j.get('meta') or {}).get('nextPageUrl')
        time.sleep(0.15)
    return out


def ghl():
    pipelines = (ghl_get(f'{GHL_BASE}/opportunities/pipelines?locationId={GHL_LOC}') or {}).get('pipelines', [])
    try:
        fields = (ghl_get(f'{GHL_BASE}/locations/{GHL_LOC}/customFields') or {}).get('customFields', [])
    except RuntimeError as e:
        fields = []
        NOTES.append(f'GHL custom field names could not be read ({str(e)[:100]}).')
    opps = ghl_pages(f'{GHL_BASE}/opportunities/search?location_id={GHL_LOC}&limit=100', 'opportunities')
    contacts = ghl_pages(f'{GHL_BASE}/contacts/?locationId={GHL_LOC}&limit=100', 'contacts')
    return pipelines, fields, opps, contacts


# Channel rules, first match wins. Applied to the contact's source, attribution and tags.
CHANNELS = [
    ('meta', r'facebook|instagram|\bmeta\b|\bfb\b|\big\b|instant ?form|lead ?form'),
    ('lsa', r'\blsa\b|local services|google guaranteed|google screened'),
    ('google_ads', r'google ads|adwords|gclid|\bcpc\b|\bppc\b|paid search|google search ads'),
    ('gbp', r'google business|\bgbp\b|\bgmb\b|google maps|my business|google profile'),
    ('website', r'website|web ?form|request.?appointment|contact ?form|web ?chat|\bchat\b|organic|\bseo\b|fullsmilechicago'),
    ('referral', r'referr|friend|family|word of mouth|existing patient'),
    ('phone', r'phone|call|walk.?in|mango'),
]
SYNC_SOURCE = re.compile(E('FS_SYNC_SOURCE_RE', r'open ?dental|od ?sync|digital ?ocean'), re.I)
JUNK_TAG = re.compile(E('FS_JUNK_TAG_RE', r'spam|junk|test|duplicate|do not contact|dnc'), re.I)
STAGES = [
    ('lost', r'lost|not interested|junk|spam|unqualified|disqualif|dnc'),
    ('noshow', r'no.?show|missed|cancel'),
    ('showed', r'showed|completed|visited|arrived|patient|won|converted'),
    ('scheduled', r'scheduled|booked|appointment'),
    ('contacted', r'contact|attempt|follow|reached|left vm|voicemail'),
    ('new', r'new'),
]


def channel_of(text):
    t = text.lower()
    for key, rx in CHANNELS:
        if re.search(rx, t):
            return key
    return 'unknown' if not t.strip() else 'other'


def ref_channel(name):
    """OpenDental "Referred from" entry to a channel. Google is left generic because OD cannot tell LSA from ads or the profile."""
    t = (name or '').lower()
    if not t:
        return ''
    if re.search(r'google', t):
        return 'google_any'
    return channel_of(t) if channel_of(t) not in ('unknown', 'other') else 'referral'


def stage_bucket(name):
    t = (name or '').lower()
    for key, rx in STAGES:
        if re.search(rx, t):
            return key
    return 'contacted'


def cf_value(contact, field_ids):
    for f in contact.get('customFields') or contact.get('customField') or []:
        if f.get('id') in field_ids:
            v = f.get('value') if 'value' in f else f.get('fieldValue')
            if isinstance(v, list):
                v = ', '.join(map(str, v))
            if v not in (None, ''):
                return str(v)
    return ''


# ---------------------------------------------------------------- build
def build():
    today = dt.date.today()
    a = (today - dt.timedelta(days=DAYS_BACK)).isoformat()
    b = today.isoformat()
    ahead = (today + dt.timedelta(days=DAYS_AHEAD)).isoformat()

    od = opendental(a, b, ahead)
    pipelines, fields, opps, contacts = ghl()

    # --- practice totals by day
    days = {}
    def row(d):
        return days.setdefault(d, {'prod': 0, 'collPat': 0, 'collIns': 0, 'done': 0, 'broken': 0, 'sched': 0, 'newdone': 0, 'newbooked': 0})
    for r in od['production']:
        if d10(r.get('d')): row(d10(r['d']))['prod'] += num(r.get('amt'))
    for r in od['collPat']:
        if d10(r.get('d')): row(d10(r['d']))['collPat'] += num(r.get('amt'))
    for r in od['collIns']:
        if d10(r.get('d')): row(d10(r['d']))['collIns'] += num(r.get('amt'))
    for r in od['appts']:
        d = d10(r.get('d'))
        if not d: continue
        x = row(d)
        for k in ('done', 'broken', 'sched', 'newdone', 'newbooked'):
            x[k] += int(num(r.get(k)))
    daily = [[d, round(x['prod'], 2), round(x['collPat'], 2), round(x['collIns'], 2), x['done'], x['broken'], x['sched'], x['newdone'], x['newbooked']]
             for d, x in sorted(days.items())]

    provs, hyg = {}, {}
    for p in od['providers']:
        name = ' '.join(filter(None, [str(p.get('f') or '').strip(), str(p.get('l') or '').strip()])) or str(p.get('abbr') or '')
        provs[str(p.get('prov'))] = name
        hyg[str(p.get('prov'))] = str(p.get('hyg')) in ('1', 'True', 'true')
    prod_cat = [[d10(r.get('wk')), r.get('cat') or 'other', round(num(r.get('amt')), 2), int(num(r.get('n')))] for r in od['prodCat'] if d10(r.get('wk'))]
    prod_prov = [[d10(r.get('wk')), str(r.get('prov')), round(num(r.get('amt')), 2)] for r in od['prodProv'] if d10(r.get('wk'))]
    used_provs = {p for _, p, _ in prod_prov}
    used_provs |= {str(r.get('prov')) for r in od.get('booked') or []} | {str(r.get('prov')) for r in od.get('sched') or []}
    providers = {k: v for k, v in provs.items() if k in used_provs}
    prov_info = {k: {'name': v, 'hygienist': hyg.get(k, False)} for k, v in providers.items()}

    first = lambda rows, k: num(rows[0].get(k)) if rows else None
    snapshot = {
        'activePatients': first(od['active'], 'n'),
        'recallOverdue': first(od['recall'], 'overdue'), 'recallDue30': first(od['recall'], 'due30'),
        'unschedPatients': first(od['unsched'], 'pats'), 'unschedAmount': first(od['unsched'], 'amt'),
        'brokenNotRebooked': first(od['brokenOpen'], 'n'),
    }
    ar = od.get('ar') or []
    if ar:
        snapshot.update({'arCurrent': first(ar, 'b0'), 'ar31': first(ar, 'b30'), 'ar61': first(ar, 'b60'), 'ar90': first(ar, 'b90'),
                         'arInsEst': first(ar, 'ins'), 'arTotal': first(ar, 'tot'), 'arFamilies': first(ar, 'fams')})
    depth = {
        'codes': [[r.get('m'), r.get('code'), str(r.get('d') or '')[:60], int(num(r.get('n'))), round(num(r.get('amt')), 2)] for r in od.get('codes') or []],
        'provDays': [[r.get('m'), str(r.get('prov')), round(num(r.get('amt')), 2), int(num(r.get('days'))), int(num(r.get('pats')))] for r in od.get('provDays') or []],
        'tx': [[r.get('m'), r.get('cat') or 'other', round(num(r.get('done')), 2), round(num(r.get('sched')), 2), round(num(r.get('open')), 2), int(num(r.get('pats')))] for r in od.get('tx') or []],
        'unschedAge': [[r.get('age'), r.get('cat') or 'other', round(num(r.get('amt')), 2), int(num(r.get('pats')))] for r in od.get('unschedAge') or []],
        'claims': [[r.get('b'), int(num(r.get('n'))), round(num(r.get('fee')), 2), round(num(r.get('est')), 2)] for r in od.get('claims') or []],
        'carriers': [[r.get('m'), str(r.get('car') or 'Unknown')[:60], round(num(r.get('billed')), 2), round(num(r.get('paid')), 2), round(num(r.get('wo')), 2), int(num(r.get('n')))] for r in od.get('carriers') or []],
        'hygiene': [[d10(r.get('wk')), int(num(r.get('n'))), int(num(r.get('rebooked')))] for r in od.get('hygiene') or [] if d10(r.get('wk'))],
        'sched': [[d10(r.get('wk')), str(r.get('prov')), round(num(r.get('hrs')), 1)] for r in od.get('sched') or [] if d10(r.get('wk'))],
        'booked': [[d10(r.get('wk')), str(r.get('prov')), round(num(r.get('hrs')), 1), int(num(r.get('done'))), int(num(r.get('sched'))), int(num(r.get('broken')))] for r in od.get('booked') or [] if d10(r.get('wk'))],
        'activeMonthly': od.get('activeMonthly') or [],
    }

    # --- GHL: who is a lead, and where they came from
    field_ids_pat, field_ids_srv = set(), set()
    for f in fields:
        blob = f"{f.get('id', '')} {f.get('fieldKey', '')} {f.get('name', '')}".lower()
        if PATNUM_FIELD and PATNUM_FIELD.lower() in blob:
            field_ids_pat.add(f.get('id'))
        elif not PATNUM_FIELD and re.search(r'patnum|patient ?id|open ?dental ?(patient|id)|od ?patient', blob):
            field_ids_pat.add(f.get('id'))
        if SERVICE_FIELD and re.search(SERVICE_FIELD, f.get('name', ''), re.I):
            field_ids_srv.add(f.get('id'))

    stage_name = {}
    for p in pipelines:
        for s in p.get('stages') or []:
            stage_name[s.get('id')] = s.get('name', '')
    opp_by_contact = {}
    for o in opps:
        cid = o.get('contactId') or (o.get('contact') or {}).get('id')
        if cid:
            opp_by_contact.setdefault(cid, []).append(o)

    by_pat, by_phone, by_email = {}, {}, {}
    leads = []
    excluded_sync = 0
    for c in contacts:
        attr = c.get('attributionSource') or {}
        last = c.get('lastAttributionSource') or {}
        tags = [str(t) for t in (c.get('tags') or [])]
        src_txt = ' '.join(str(x) for x in [c.get('source'), attr.get('utmSource'), attr.get('utmMedium'), attr.get('campaign'),
                                            attr.get('sessionSource'), attr.get('medium'), last.get('utmSource'), last.get('sessionSource'), attr.get('url')] if x)
        name = f"{c.get('firstName') or ''} {c.get('lastName') or ''}".strip().lower()
        sync_made = bool(SYNC_SOURCE.search(str(c.get('source') or ''))) or name.startswith('od patient') \
            or (not src_txt.strip() and any('open-dental-synced' in t for t in tags))
        junk = any(JUNK_TAG.search(t) for t in tags)
        ch = channel_of(src_txt + ' ' + ' '.join(t for t in tags if not re.search(r'patient|open-dental', t)))
        pat = cf_value(c, field_ids_pat) if field_ids_pat else ''
        rec = {'id': c.get('id'), 'ch': ch, 'lead': not sync_made and not junk, 'day': d10(c.get('dateAdded')),
               'service': (cf_value(c, field_ids_srv) if field_ids_srv else '').strip()[:40]}
        if re.sub(r'\D', '', pat): by_pat[re.sub(r'\D', '', pat)] = rec
        if digits10(c.get('phone')): by_phone.setdefault(digits10(c.get('phone')), rec)
        if c.get('email'): by_email.setdefault(str(c['email']).strip().lower(), rec)
        if sync_made:
            excluded_sync += 1
            continue
        if junk or not rec['day'] or rec['day'] < a:
            continue
        # Furthest stage the lead reached in GHL (fallback when OpenDental has no appointment for them yet).
        best = 'new'
        order = ['new', 'contacted', 'scheduled', 'showed']
        for o in opp_by_contact.get(c.get('id'), []):
            bkt = stage_bucket(stage_name.get(o.get('pipelineStageId'), ''))
            if o.get('status') == 'won':
                bkt = 'showed'
            if bkt in order and order.index(bkt) > order.index(best):
                best = bkt
            if bkt in ('lost', 'noshow') and best == 'new':
                best = bkt
        rec['stage'] = best
        rec['untouched'] = best == 'new' and all(stage_bucket(stage_name.get(o.get('pipelineStageId'), '')) == 'new' for o in opp_by_contact.get(c.get('id'), [])) \
            and rec['day'] <= (today - dt.timedelta(days=1)).isoformat() and rec['day'] >= (today - dt.timedelta(days=30)).isoformat()
        leads.append(rec)

    def match(r):
        p = re.sub(r'\D', '', str(r.get('pat') or ''))
        if p and p in by_pat: return by_pat[p], 'patnum'
        for k in ('m', 'h', 'w'):
            ph = digits10(r.get(k))
            if ph and ph in by_phone: return by_phone[ph], 'phone'
        em = str(r.get('e') or '').strip().lower()
        if em and em in by_email: return by_email[em], 'email'
        return None, ''

    # --- OpenDental truth for each lead: booked / showed
    booked_ids, showed_ids = set(), set()
    for r in od['newAppts']:
        rec, _ = match(r)
        if not rec: continue
        booked_ids.add(rec['id'])
        if str(r.get('st')) == '2': showed_ids.add(rec['id'])

    # --- new patients by source (first visit week), with what they have paid so far
    ref_by_pat = {}
    for r in od['ref']:
        ref_by_pat.setdefault(str(r.get('pat')), ' '.join(filter(None, [str(r.get('f') or ''), str(r.get('l') or '')])).strip())
    coll = {str(r.get('pat')): num(r.get('amt')) for r in od['npColl']}
    prod = {str(r.get('pat')): num(r.get('amt')) for r in od['npProd']}
    plan = {str(r.get('pat')): num(r.get('amt')) for r in od['npPlan']}
    visits = {str(r.get('pat')): int(num(r.get('n'))) for r in od.get('npVisits') or []}
    np_weeks, quality = {}, {'patnum': 0, 'phone': 0, 'email': 0, 'odReferral': 0, 'none': 0, 'leadAfterVisit': 0}
    lead_np, AUDIT = {}, []
    for r in od['newPatients']:
        fv = d10(r.get('fv'))
        if not fv: continue
        rec, how = match(r)
        pat = str(r.get('pat'))
        # Credit a channel only when the GHL lead existed before the first visit (and within a year of it).
        # A contact created after the visit is a patient who later landed in GHL, not a patient the channel brought.
        fv_d = dt.date.fromisoformat(fv)
        lead_ok = bool(rec and rec['lead'] and rec['day'] and rec['day'] <= fv and rec['day'] >= (fv_d - dt.timedelta(days=365)).isoformat())
        if rec and rec['lead'] and not lead_ok:
            quality['leadAfterVisit'] += 1
        if lead_ok:
            ch, by = rec['ch'], 'ghl'
            quality[how] += 1
            showed_ids.add(rec['id'])
            t = lead_np.setdefault(rec['id'], [0, 0.0]); t[0] += 1; t[1] += coll.get(pat, 0)
            AUDIT.append((ch, pat, fv, rec['day'], how, round(coll.get(pat, 0), 2)))
        elif ref_by_pat.get(pat):
            ch, by = ref_channel(ref_by_pat[pat]) or 'referral', 'opendental'
            quality['odReferral'] += 1
        else:
            ch, by = 'not_in_ghl', 'none'
            quality['none'] += 1
        key = (monday(fv), ch, by)
        x = np_weeks.setdefault(key, [0, 0.0, 0.0, 0.0, 0])
        x[0] += 1; x[1] += coll.get(pat, 0); x[2] += prod.get(pat, 0); x[3] += plan.get(pat, 0)
        if visits.get(pat, 0) >= 2: x[4] += 1
    new_patients = [[w, ch, by, v[0], round(v[1], 2), round(v[2], 2), round(v[3], 2), v[4]] for (w, ch, by), v in sorted(np_weeks.items())]

    # --- leads by week and channel (cohort by the week the lead came in)
    # Columns: leads, booked, showed, lost, then OpenDental-confirmed: new patients from these leads, and what they have paid.
    lw = {}
    for l in leads:
        stage = 'showed' if l['id'] in showed_ids else 'scheduled' if l['id'] in booked_ids else l['stage']
        key = (monday(l['day']), l['ch'])
        x = lw.setdefault(key, [0, 0, 0, 0, 0, 0.0])
        x[0] += 1
        if stage in ('scheduled', 'showed', 'noshow'): x[1] += 1
        if stage == 'showed': x[2] += 1
        if stage == 'lost': x[3] += 1
        if l['id'] in lead_np: x[4] += lead_np[l['id']][0]; x[5] = round(x[5] + lead_np[l['id']][1], 2)
    lead_weeks = [[w, ch, *v] for (w, ch), v in sorted(lw.items())]
    ld = {}
    for l in leads:
        ld[(l['day'], l['ch'])] = ld.get((l['day'], l['ch']), 0) + 1
    lead_days = [[d, ch, n] for (d, ch), n in sorted(ld.items())]
    sv = {}
    for l in leads:
        if l['service']:
            k = (monday(l['day']), l['ch'], l['service'].lower())
            sv[k] = sv.get(k, 0) + 1
    services = [[w, ch, s, n] for (w, ch, s), n in sorted(sv.items())]

    # --- pipeline right now (counts only)
    open_stage = {}
    for o in opps:
        if o.get('status') not in (None, 'open'): continue
        nm = stage_name.get(o.get('pipelineStageId'), 'Unknown')
        open_stage[nm] = open_stage.get(nm, 0) + 1
    waiting = {
        'untouched': sum(1 for l in leads if l.get('untouched') and l['id'] not in booked_ids),
        'contactedNotBooked': sum(1 for l in leads if l['stage'] == 'contacted' and l['id'] not in booked_ids and l['day'] >= (today - dt.timedelta(days=30)).isoformat()),
        'noShowNotRebooked': sum(1 for l in leads if l['stage'] == 'noshow' and l['id'] not in showed_ids and l['day'] >= (today - dt.timedelta(days=60)).isoformat()),
    }

    globals()['AUDIT_ROWS'] = AUDIT
    if not field_ids_pat:
        NOTES.append('No GHL custom field holding the OpenDental patient ID was found, so matching used phone and email only. Set GHL_PATNUM_FIELD.')
    return {
        'version': 2, 'generatedAt': dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z', 'asOf': b, 'window': [a, b],
        'ghl': {'location': GHL_LOC},
        'practice': {'daily': daily, 'prodCat': prod_cat, 'prodProv': prod_prov, 'providers': providers, 'providerInfo': prov_info,
                     'snapshot': snapshot, **depth},
        'newPatients': new_patients,
        'leads': {'weeks': lead_weeks, 'days': lead_days, 'services': services, 'openStages': sorted(open_stage.items(), key=lambda x: -x[1]), 'waiting': waiting},
        'quality': {**quality, 'syncContactsExcluded': excluded_sync, 'leads': len(leads), 'patnumField': bool(field_ids_pat)},
        'notes': NOTES,
    }


# ---------------------------------------------------------------- publish
def gh(method, path, body=None):
    return http(method, f'https://api.github.com/repos/{GH_REPO}/{path}'.rstrip('/'),
                {'Authorization': f'Bearer {GH_TOKEN}', 'Accept': 'application/vnd.github+json', 'Content-Type': 'application/json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'fullsmile-metrics'}, body)


def publish(doc):
    text = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    sha, same = None, False
    try:
        cur = gh('GET', f'contents/{GH_PATH}?ref={GH_BRANCH}')
        sha = cur.get('sha')
        old = json.loads(base64.b64decode(cur.get('content', '')).decode() or '{}')
        hist = dict(old.get('history') or {})
        hist[doc['asOf']] = doc['practice']['snapshot']
        doc['history'] = {k: hist[k] for k in sorted(hist)[-400:]}
        text = json.dumps(doc, separators=(',', ':'), sort_keys=True)
        old.pop('generatedAt', None)
        new = dict(doc); new.pop('generatedAt', None)
        same = json.dumps(old, sort_keys=True) == json.dumps(new, sort_keys=True)
    except RuntimeError as e:
        if '404' not in str(e):
            raise
    if 'history' not in doc:
        doc['history'] = {doc['asOf']: doc['practice']['snapshot']}
        text = json.dumps(doc, separators=(',', ':'), sort_keys=True)
    if same:
        print('No change since the last push; nothing committed.')
        return
    body = {'message': f"Practice metrics {doc['asOf']}", 'content': base64.b64encode(text.encode()).decode(), 'branch': GH_BRANCH}
    if sha:
        body['sha'] = sha
    gh('PUT', f'contents/{GH_PATH}', body)
    print(f'Pushed {GH_PATH} to {GH_REPO}.')


def check():
    ok = True
    try:
        r = od_query('SELECT COUNT(*) AS n FROM patient')
        print('OpenDental: OK,', r[0].get('n') if r else '?', 'patient records')
    except Exception as e:
        ok = False; print('OpenDental: FAILED ->', e)
    try:
        p = (ghl_get(f'{GHL_BASE}/opportunities/pipelines?locationId={GHL_LOC}') or {}).get('pipelines', [])
        print('GHL: OK,', len(p), 'pipelines')
    except Exception as e:
        ok = False; print('GHL: FAILED ->', e)
    try:
        gh('GET', '')
        print('GitHub: OK,', GH_REPO)
    except Exception as e:
        ok = False; print('GitHub: FAILED ->', e)
    return ok


if __name__ == '__main__':
    missing = [k for k in ('GHL_TOKEN', 'GHL_LOCATION_ID') if not E(k)] + ([] if E('OD_AUTH') or (E('OD_DEVELOPER_KEY') and E('OD_CUSTOMER_KEY')) else ['OD_DEVELOPER_KEY/OD_CUSTOMER_KEY'])
    if missing:
        sys.exit('Missing settings: ' + ', '.join(missing))
    if '--check' in sys.argv:
        sys.exit(0 if check() else 1)
    started = time.time()
    doc = build()
    q = doc['quality']
    print(f"Built: {len(doc['practice']['daily'])} days, {sum(r[3] for r in doc['newPatients'])} new patients (OpenDental new-patient visits) "
          f"(matched to GHL by PatNum {q['patnum']}, phone {q['phone']}, email {q['email']}; OD referral {q['odReferral']}; none {q['none']}), "
          f"{q['leads']} leads, {round(time.time() - started)}s")
    for n in NOTES:
        print('note:', n)
    if '--audit' in sys.argv:
        # Printed on this screen only; never written to a file or pushed. PatNums let staff spot-check in OpenDental.
        rows = sorted(globals().get('AUDIT_ROWS', []), key=lambda x: (x[0], x[2]))
        want = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--channel=')]
        print('channel | PatNum | first visit | lead created | matched by | collected so far')
        for ch, pat, fv, ld, how, c in rows:
            if not want or ch in want:
                print(f'{ch} | {pat} | {fv} | {ld} | {how} | {c}')
        sys.exit(0)
    if '--dry-run' in sys.argv:
        open(os.path.join(HERE, 'practice.json'), 'w').write(json.dumps(doc, indent=1))
        print('Wrote practice.json (not pushed).')
    else:
        if not GH_TOKEN:
            sys.exit('Missing GITHUB_TOKEN.')
        publish(doc)

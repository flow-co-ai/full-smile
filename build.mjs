// Builds the Full Smile dashboard data file.
//   data/practice.json  — totals from OpenDental and GHL, written by the droplet (no patient details)
//   Windsor             — Meta, Google Ads, Google profile, Search Console, Instagram
//   data/lsa-spend.json — LSA spend by month, entered by hand
// Output: public/data.enc.json (AES-256-GCM, opened in the browser with the dashboard password).
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import zlib from 'node:zlib';
import { fetchWindsor } from './windsor.mjs';

const cfg = JSON.parse(await fs.readFile('config.json', 'utf8'));
const notes = [];
const num = v => { const n = parseFloat(String(v ?? '').replace(/[$,\s]/g, '')); return isNaN(n) ? 0 : n; };
const R = n => Math.round(n * 100) / 100;
const day = v => String(v || '').slice(0, 10);
const low = s => String(s || '').trim().toLowerCase();
const shift = (d, n) => { const x = new Date(`${d}T12:00:00Z`); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
const monday = d => { const x = new Date(`${d}T12:00:00Z`); x.setUTCDate(x.getUTCDate() - ((x.getUTCDay() + 6) % 7)); return x.toISOString().slice(0, 10); };

const now = new Date();
const today = new Intl.DateTimeFormat('en-CA', { timeZone: cfg.timezone || 'America/Chicago' }).format(now);
const yesterday = shift(today, -1);
const gscTo = shift(today, -3);
const w = {
  from: shift(yesterday, -394), to: yesterday, d28from: shift(yesterday, -27), igFrom: shift(today, -720),
  gscTo, gscFrom: shift(gscTo, -27), gscPrevTo: shift(gscTo, -28), gscPrevFrom: shift(gscTo, -55), gscHistFrom: shift(gscTo, -181),
};

// ---------- inputs
const fixture = process.env.FIXTURE ? JSON.parse(await fs.readFile(process.env.FIXTURE, 'utf8')) : null;
const raw = fixture ? fixture.windsor : await fetchWindsor(cfg.windsor, process.env.WINDSOR_API_KEY, w, notes);
let practice = null;
try { practice = JSON.parse(await fs.readFile(fixture ? fixture.practicePath : 'data/practice.json', 'utf8')); }
catch { notes.push('No practice data from the droplet yet (data/practice.json). OpenDental and GHL numbers will appear after its first run.'); }
if (practice?.asOf && practice.asOf < shift(today, -2)) notes.push(`Practice data is from ${practice.asOf}. The droplet has not pushed an update since then.`);
let lsa = { months: {} };
try { lsa = JSON.parse(await fs.readFile('data/lsa-spend.json', 'utf8')); } catch { notes.push('data/lsa-spend.json is missing, so LSA spend reads zero.'); }

// ---------- spend by day and channel
const spend = [];
for (const r of raw.meta || []) if (num(r.spend)) spend.push([day(r.date), 'meta', R(num(r.spend))]);
for (const r of raw.gads || []) if (num(r.spend)) spend.push([day(r.date), /local services|lsa/i.test(r.campaign || '') ? 'lsa' : 'google_ads', R(num(r.spend))]);
const lsaMonths = lsa.months || {};
for (const [ym, amt] of Object.entries(lsaMonths)) {
  if (!num(amt)) continue;
  const [y, m] = ym.split('-').map(Number), dim = new Date(Date.UTC(y, m, 0)).getUTCDate();
  for (let dd = 1; dd <= dim; dd++) spend.push([`${ym}-${String(dd).padStart(2, '0')}`, 'lsa', R(num(amt) / dim)]);
}
const lastLsaMonth = Object.keys(lsaMonths).sort().pop() || null;

// ---------- Meta ads (per ad per day) and lead-form counts
const ads = {}, adRows = [];
for (const r of raw.meta || []) {
  const k = r.ad_id || r.ad_name; if (!k) continue;
  ads[k] ||= { name: r.ad_name || k, campaign: r.campaign || '', adset: r.adset_name || '' };
  adRows.push([day(r.date), k, R(num(r.spend)), num(r.actions_lead), num(r.clicks)]);
}
const formRows = {};
for (const r of raw.fbLeads || []) { const k = `${day(r.created_time)}|${r.ad_name || 'Unknown ad'}`; formRows[k] = (formRows[k] || 0) + 1; }
const forms = Object.entries(formRows).map(([k, n]) => [...k.split('|'), n]);
const gadsRows = (raw.gads || []).filter(r => num(r.spend) || num(r.clicks)).map(r => [day(r.date), r.campaign || '', R(num(r.spend)), num(r.clicks), num(r.conversions)]);

// ---------- Google profile and reviews
const gbpDaily = new Map();
for (const r of raw.gmb || []) { const d = day(r.date); if (!d) continue; const t = gbpDaily.get(d) || gbpDaily.set(d, [d, 0, 0, 0, 0]).get(d); t[1] += num(r.impressions); t[2] += num(r.call_clicks); t[3] += num(r.direction_requests); t[4] += num(r.website_clicks); }
const STARS = { ONE: 1, TWO: 2, THREE: 3, FOUR: 4, FIVE: 5 };
const seen = new Set(), revList = []; let revTotal = null;
for (const r of raw.reviews || []) {
  if (num(r.review_total_count)) revTotal = Math.max(revTotal || 0, num(r.review_total_count));
  const d = day(r.review_create_time); if (!r.review_id || !d || seen.has(r.review_id)) continue; seen.add(r.review_id);
  revList.push([d, STARS[String(r.review_star_rating || '').toUpperCase()] || null]);
}
revList.sort((a, b) => a[0].localeCompare(b[0]));
const rated = revList.filter(x => x[1]);

// ---------- Search Console (query-level positions, impression-weighted)
const agg = rows => { const m = new Map(); for (const r of rows || []) { if (!r.query) continue; const q = m.get(r.query) || m.set(r.query, { q: r.query, c: 0, i: 0, p: 0 }).get(r.query); const i = num(r.impressions); q.c += num(r.clicks); q.i += i; q.p += num(r.position) * i; } return m; };
const pos = x => (x && x.i ? +(x.p / x.i).toFixed(1) : null);
const cur = agg(raw.gscCur), prev = agg(raw.gscPrev), brand = new RegExp(cfg.brand_regex || '$^', 'i');
const qs = [...cur.values()].map(x => ({ q: x.q, c: Math.round(x.c), i: Math.round(x.i), pos: pos(x), prev: prev.get(x.q) ? pos(prev.get(x.q)) : null, brand: brand.test(x.q) }));
const hist = new Map(), weekSet = new Set();
for (const r of raw.gscHist || []) {
  if (!r.query || !r.date) continue; const wk = monday(day(r.date)), i = num(r.impressions); weekSet.add(wk);
  const q = hist.get(r.query) || hist.set(r.query, new Map()).get(r.query), t = q.get(wk) || q.set(wk, { i: 0, p: 0 }).get(wk); t.i += i; t.p += num(r.position) * i;
}
const weeks = [...weekSet].sort(), tracked = (cfg.tracked_keywords || []).map(low), byQ = new Map(qs.map(x => [x.q, x]));
const series = {};
for (const q of new Set([...tracked, ...qs.filter(x => !x.brand && x.i >= 20).map(x => x.q)])) { const m = hist.get(q); if (m) series[q] = { p: weeks.map(wk => { const t = m.get(wk); return t && t.i ? +(t.p / t.i).toFixed(1) : null; }) }; }
const gscDaily = new Map();
for (const r of raw.gscDaily || []) { const d = day(r.date), t = gscDaily.get(d) || gscDaily.set(d, [d, 0, 0]).get(d); t[1] += num(r.clicks); t[2] += num(r.impressions); }
const gsc = {
  tracked: tracked.map(q => byQ.get(q) || { q, c: 0, i: 0, pos: null, prev: null }), weeks, series,
  almost: qs.filter(x => !x.brand && x.pos > 10.5 && x.pos <= 20.5 && x.i >= 10).sort((a, b) => b.i - a.i).slice(0, 12),
  top: qs.filter(x => !x.brand).sort((a, b) => b.c - a.c || b.i - a.i).slice(0, 40),
  daily: [...gscDaily.values()].sort((a, b) => a[0].localeCompare(b[0])), window: [w.gscFrom, w.gscTo],
};

// ---------- Instagram
let ig = null;
if ((raw.igHist || []).length || (raw.igMedia || []).length) {
  const dm = new Map(), row = d => dm.get(d) || dm.set(d, [d, null, null, 0, 0, 0]).get(d);
  for (const r of raw.igHist || []) { const d = day(r.date); if (!d) continue; const t = row(d); t[3] += num(r.views); t[4] += num(r.total_interactions); t[5] += num(r.accounts_engaged); }
  for (const r of raw.igRecent || []) { const d = day(r.date); if (!d) continue; const t = row(d); t[1] = (t[1] || 0) + num(r.reach_1d); t[2] = (t[2] || 0) + num(r.follower_count_1d); }
  const tidy = c => { const first = String(c || '').split(/\n/).map(x => x.trim()).find(x => x && !/^#/.test(x)) || ''; const t = first.replace(/#\w+/g, '').replace(/\s+/g, ' ').trim(); return t.length > 90 ? t.slice(0, t.lastIndexOf(' ', 88)).replace(/[,.;:!?-]+$/, '') + '…' : t; };
  const ps = new Set(), posts = [];
  for (const r of raw.igMedia || []) { if (!r.media_id || ps.has(r.media_id)) continue; ps.add(r.media_id); posts.push({ d: day(r.date), type: r.media_type || '', url: r.media_permalink || '', cap: tidy(r.media_caption), reach: Math.round(num(r.media_reach)), views: Math.round(num(r.media_views)), shares: Math.round(num(r.media_shares)) }); }
  const fol = (raw.igNow || []).map(r => num(r.followers_count)).filter(Boolean);
  const daily = [...dm.values()].filter(r => r[3] || r[4] || r[1] != null).sort((a, b) => a[0].localeCompare(b[0]));
  ig = { daily, from: daily[0]?.[0] || null, posts: posts.sort((a, b) => b.d.localeCompare(a.d)), followers: fol.length ? Math.max(...fol) : null, handle: cfg.instagram_handle || '' };
}

// ---------- result
const result = {
  generatedAt: now.toISOString(), today, asOf: yesterday, name: cfg.practice_name, ghlUrl: cfg.ghl_opportunities_url || '',
  ghl: { base: (cfg.ghl_app_base || 'https://app.gohighlevel.com').replace(/\/$/, ''), location: practice?.ghl?.location || cfg.ghl_location_id || '' },
  adsActive: cfg.ads_active !== false,
  practice, spend, lsa: { lastMonth: lastLsaMonth, cap: cfg.lsa_monthly_cap || null },
  meta: { ads, rows: adRows, forms }, gads: gadsRows,
  gbp: { daily: [...gbpDaily.values()].sort((a, b) => a[0].localeCompare(b[0])), reviews: { total: revTotal, list: revList, avg: rated.length ? +(rated.reduce((s, x) => s + x[1], 0) / rated.length).toFixed(2) : null } },
  gsc, ig, notes: [...notes, ...(practice?.notes || [])],
};

function encrypt(obj, pass) {
  const salt = crypto.randomBytes(16), iv = crypto.randomBytes(12), iter = 200000;
  const key = crypto.pbkdf2Sync(pass, salt, iter, 32, 'sha256');
  const c = crypto.createCipheriv('aes-256-gcm', key, iv);
  const body = Buffer.concat([c.update(zlib.gzipSync(JSON.stringify(obj))), c.final(), c.getAuthTag()]);
  return { v: 1, gz: true, iter, salt: salt.toString('base64'), iv: iv.toString('base64'), ct: body.toString('base64') };
}
const OUT = path.resolve('public');
await fs.rm(path.join(OUT, 'data.json'), { force: true });
await fs.rm(path.join(OUT, 'data.enc.json'), { force: true });
if (process.env.NO_ENCRYPT === '1') { await fs.writeFile(path.join(OUT, 'data.json'), JSON.stringify(result)); console.log('Wrote public/data.json (UNENCRYPTED, local preview only)'); }
else {
  if (!process.env.DASHBOARD_KEY) throw new Error('DASHBOARD_KEY is required (or NO_ENCRYPT=1 for local preview).');
  await fs.writeFile(path.join(OUT, 'data.enc.json'), JSON.stringify(encrypt(result, process.env.DASHBOARD_KEY)));
  console.log('Wrote public/data.enc.json');
}
console.log(`as of ${yesterday} · practice data ${practice ? practice.asOf : 'missing'} · ${spend.length} spend rows · ${Object.keys(ads).length} Meta ads · ${revList.length} reviews`);
notes.forEach(n => console.log('note:', n));

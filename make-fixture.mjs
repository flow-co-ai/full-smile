// Sample data for local preview only. Fake numbers, no real patients.
import fs from 'node:fs/promises';
let seed = 11; const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const pick = a => a[Math.floor(rnd() * a.length)];
const shift = (d, n) => { const x = new Date(`${d}T12:00:00Z`); x.setUTCDate(x.getUTCDate() + n); return x.toISOString().slice(0, 10); };
const TODAY = new Date().toISOString().slice(0, 10), END = shift(TODAY, -1);
const days = []; for (let d = shift(END, -394); d <= END; d = shift(d, 1)) days.push(d);
const meta = [], gads = [], gmb = [], reviews = [], gscDaily = [], fbLeads = [], igHist = [], igRecent = [], igMedia = [];
const ADS = [['a1', 'Metal-Free Implants | Prospecting', 'Implants 2026'], ['a2', 'Back to School Checkups', 'Q4 Seasonal'], ['a3', 'Use Your Benefits Before Dec 31', 'Q4 Seasonal'], ['a4', 'Retargeting | Site Visitors', 'Retargeting']];
for (const d of days) {
  const i = days.indexOf(d), live = d >= '2026-02-01';
  if (live) for (const [id, name, camp] of ADS) { if (id !== 'a1' && d < '2026-08-15') continue; const s = +(5 + rnd() * 5).toFixed(2); meta.push({ date: d, campaign: camp, adset_name: camp, ad_name: name, ad_id: id, spend: s, clicks: Math.round(s * 1.4), impressions: Math.round(s * 110), actions_lead: rnd() < (id === 'a1' ? 0.35 : 0.2) ? 1 : 0 }); }
  if (d >= '2026-02-01' && d <= '2026-06-19') for (const c of ['Search - Call Optimised', 'Search | Dental Implants']) gads.push({ date: d, campaign: c, spend: +(8 + rnd() * 10).toFixed(2), clicks: Math.round(2 + rnd() * 4), impressions: 90, conversions: rnd() < 0.2 ? 1 : 0 });
  gmb.push({ date: d, impressions: Math.round(160 + rnd() * 80 + i / 3), call_clicks: Math.round(rnd() * 5), direction_requests: Math.round(rnd() * 4), website_clicks: Math.round(rnd() * 6) });
  if (rnd() < 0.07) reviews.push({ review_id: `r${i}`, review_create_time: `${d}T15:00:00Z`, review_star_rating: d === '2026-07-17' ? 'ONE' : 'FIVE', review_total_count: 27 });
  gscDaily.push({ date: d, clicks: Math.round(3 + rnd() * 8), impressions: Math.round(300 + rnd() * 400) });
  if (live && rnd() < 0.3) fbLeads.push({ id: `l${i}`, created_time: `${d}T14:00:00Z`, campaign: 'Implants 2026', ad_name: pick(ADS)[1], form_name: 'Implant consult' });
  igHist.push({ date: d, views: Math.round(200 + rnd() * 500 + i), total_interactions: Math.round(5 + rnd() * 20), accounts_engaged: Math.round(4 + rnd() * 12) });
  if (i >= days.length - 29) igRecent.push({ date: d, reach_1d: Math.round(80 + rnd() * 160), follower_count_1d: Math.floor(rnd() * 3) });
  if (i >= days.length - 365 && rnd() < 0.2) igMedia.push({ media_id: `m${i}`, date: d, media_type: 'REELS', media_permalink: `https://www.instagram.com/reel/sample${i}/`, media_caption: pick(['Meet Dr. Adham\n#fullsmile', 'What a deep cleaning actually does', 'Metal-free implants, explained', 'Back to school smiles']), media_reach: Math.round(150 + rnd() * 500), media_views: Math.round(300 + rnd() * 900), media_engagement: 10, media_shares: Math.floor(rnd() * 4), media_saved: 1 });
}
const Q = [['full smile dental worth', 1.9, 349], ['dentist worth il', 9.8, 80], ['dentist in worth il', 7, 40], ['emergency dentist worth il', 14.2, 60], ['painless emergency dentist worth il', 1, 17], ['dental implants worth il', 18.5, 55], ['metal free dental implants', 42, 73], ['ceramic dental implants', 38, 64], ['sleep apnea dentist near me', 31, 70], ['dental deep cleaning near me', 30, 57], ['dental sealants near me', 26.8, 95], ['extractions near me', 34.6, 194], ['dentist near me open saturday', 16, 88], ['family dentist palos hills', 13, 42]];
const gscCur = Q.map(([q, p, i]) => ({ query: q, clicks: Math.round(i * (p < 10 ? 0.08 : 0.005)), impressions: i, position: p }));
const gscPrev = Q.map(([q, p, i]) => ({ query: q, clicks: 0, impressions: Math.round(i * 0.8), position: p + 3 }));
const gscHist = [];
for (const d of days.slice(-182)) for (const [q, p, i] of Q) { const k = (days.length - days.indexOf(d)) / 182; gscHist.push({ date: d, query: q, clicks: 0, impressions: Math.round(i / 28), position: +(p + k * 8 + rnd()).toFixed(1) }); }
await fs.mkdir('sample', { recursive: true });
await fs.writeFile('sample/fixture.json', JSON.stringify({ practicePath: 'sample/practice.json', windsor: { meta, fbLeads, gads, gmb, reviews, gscDaily, gscCur, gscPrev, gscHist, igHist, igRecent, igMedia, igNow: [{ followers_count: 612 }] } }));
console.log('fixture written');

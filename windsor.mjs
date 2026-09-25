// Windsor.ai connector API. Same key as the other Flow Company dashboards.
// Nothing here carries patient data: ad spend, Google profile, Search Console and Instagram only.
const BASE = 'https://connectors.windsor.ai';

async function call(connector, fields, accounts, from, to, key) {
  const params = new URLSearchParams({ api_key: key, fields: fields.join(','), date_from: from, date_to: to, select_accounts: accounts.join(','), _max_rows: '100000' });
  const res = await fetch(`${BASE}/${connector}?${params}`);
  if (!res.ok) throw new Error(`${res.status} ${(await res.text().catch(() => '')).slice(0, 140)}`);
  const json = await res.json();
  return Array.isArray(json.data) ? json.data : [];
}

const META = ['date', 'campaign', 'adset_name', 'ad_name', 'ad_id', 'spend', 'clicks', 'impressions', 'actions_lead'];
// Lead-form rows: only the ad and the date, never the person's answers or contact details.
const FB_LEADS = ['id', 'created_time', 'campaign', 'ad_name', 'form_name'];
const GADS = ['date', 'campaign', 'spend', 'clicks', 'impressions', 'conversions'];
const GMB = ['date', 'impressions', 'call_clicks', 'direction_requests', 'website_clicks'];
const REVIEWS = ['review_id', 'review_create_time', 'review_star_rating', 'review_total_count'];
const IG_HIST = ['date', 'views', 'total_interactions', 'accounts_engaged'];
const IG_RECENT = ['date', 'reach_1d', 'follower_count_1d'];
const IG_MEDIA = ['media_id', 'date', 'media_type', 'media_permalink', 'media_caption', 'media_reach', 'media_views', 'media_engagement', 'media_shares', 'media_saved'];

export async function fetchWindsor(acct, key, w, notes) {
  const safe = async (label, connector, fields, accounts, from, to) => {
    if (!accounts?.length) return [];
    try { return await call(connector, fields, accounts, from, to, key); }
    catch (e) { notes.push(`${label} could not be read from Windsor (${e.message.slice(0, 100)}).`); return []; }
  };
  const [meta, fbLeads, gads, gmb, reviews, gscDaily, gscCur, gscPrev, gscHist, igHist, igRecent, igMedia, igNow] = await Promise.all([
    safe('Meta ads', 'facebook', META, acct.facebook, w.from, w.to),
    safe('Meta lead forms', 'facebook_leads', FB_LEADS, acct.facebook_leads, w.from, w.to),
    safe('Google Ads', 'google_ads', GADS, acct.google_ads, w.from, w.to),
    safe('Google Business Profile', 'google_my_business', GMB, acct.google_my_business, w.from, w.to),
    safe('Google reviews', 'google_my_business', REVIEWS, acct.google_my_business, w.from, w.to),
    safe('Search Console daily', 'searchconsole', ['date', 'clicks', 'impressions'], acct.searchconsole, w.from, w.gscTo),
    // Query-level only: splitting by page skews average position.
    safe('Search Console queries', 'searchconsole', ['query', 'clicks', 'impressions', 'position'], acct.searchconsole, w.gscFrom, w.gscTo),
    safe('Search Console prior queries', 'searchconsole', ['query', 'clicks', 'impressions', 'position'], acct.searchconsole, w.gscPrevFrom, w.gscPrevTo),
    safe('Search Console ranking history', 'searchconsole', ['date', 'query', 'clicks', 'impressions', 'position'], acct.searchconsole, w.gscHistFrom, w.gscTo),
    safe('Instagram history', 'instagram', IG_HIST, acct.instagram, w.igFrom, w.to),
    safe('Instagram reach', 'instagram', IG_RECENT, acct.instagram, w.d28from, w.to),
    safe('Instagram posts', 'instagram', IG_MEDIA, acct.instagram, w.from, w.to),
    safe('Instagram followers', 'instagram', ['followers_count'], acct.instagram, w.d28from, w.to),
  ]);
  return { meta, fbLeads, gads, gmb, reviews, gscDaily, gscCur, gscPrev, gscHist, igHist, igRecent, igMedia, igNow };
}

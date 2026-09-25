# Full Smile Dental: practice report

Private dashboard at **fullsmile-practice-report.netlify.app** (Netlify site `97a208f2-8ffd-444b-b21d-12a711a3bc82`).
Seven views: Overview, New patients, Production, Treatment, Hygiene & schedule, Online presence, Follow-ups.
Every number opens a sidebar with its month-by-month detail. Password-protected (AES-256-GCM, decrypted in the browser).

GHL buttons open the Full Smile sub-account (the droplet sends the location ID; set `ghl_app_base` in config.json if GHL
runs on a white-label domain). OpenDental is desktop software, so each list names its menu path instead of linking.
Ads are off (`ads_active: false`), so spend no longer drives the headlines; historic spend stays in the data.

## How it fits together

```
OpenDental ─┐                         (droplet, HIPAA side)
GHL ────────┴─ droplet/fullsmile_metrics.py ── totals only ──► data/practice.json (this repo)
                                                                        │
Windsor (Meta, Google Ads, Google profile, Search Console, Instagram) ──┤
data/lsa-spend.json (entered monthly) ──────────────────────────────────┤
                                                                        ▼
                                             GitHub Action: scripts/build.mjs → encrypted → Netlify
```

**Patient data never leaves the droplet.** The droplet matches new patients to their GHL lead itself and sends only
counts and dollar totals (by day for practice totals, by week for anything tied to patients). GitHub and Netlify
never see a name, phone, email, patient ID or visit record.

## Secrets (GitHub → Settings → Secrets and variables → Actions)

| Secret | Value |
|---|---|
| `WINDSOR_API_KEY` | Same Windsor key as the other dashboards |
| `DASHBOARD_KEY` | The password Dr. Jamal and Dr. Adham will type |
| `NETLIFY_AUTH_TOKEN` | Same Netlify token as the Maadi dashboard |
| `NETLIFY_SITE_ID` | `97a208f2-8ffd-444b-b21d-12a711a3bc82` |

## Monthly: LSA spend

Google LSA is billed outside the Google Ads account Windsor reads. At the start of each month, open the LSA dashboard,
copy last month's total into `data/lsa-spend.json` (`"2026-09": 1234`), and commit. The Attention tab reminds you
when a month is missing. Optional: set `lsa_monthly_cap` in `config.json` to get a warning at 80%.

## Settings (config.json)

- `tracked_keywords`: the local searches on the Reputation tab.
- `ghl_opportunities_url`: paste the GHL Opportunities page URL so "Open GHL" buttons go straight there.
- `windsor`: account ids (verified: Meta 1222692849377651, lead forms 986536337875135, Google Ads 256-681-8994,
  Google profile locations/9295260348937485711, Search Console https://fullsmilechicago.com/, Instagram 17841477163064218).

## Local preview (sample numbers, no real patients)

```bash
node scripts/make-fixture.mjs
cp droplet/practice.json sample/practice.json   # from: python3 droplet/fullsmile_metrics.py --dry-run
FIXTURE=sample/fixture.json NO_ENCRYPT=1 node scripts/build.mjs
python3 -m http.server -d public 8080
```

Droplet setup is in `droplet/README.md`.

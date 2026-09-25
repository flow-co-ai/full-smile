# Droplet script (for Ahmed / Ali)

`fullsmile_metrics.py` runs on the Full Smile droplet (134.122.28.124), next to the existing OpenDental ↔ GHL sync.
It reads OpenDental (Open Dental API, read-only SQL via `PUT /queries/ShortQuery`) and GHL, matches new patients
to their GHL lead on the droplet, and commits **only totals** to `data/practice.json` in the dashboard repo.
No names, phones, emails, PatNums or per-patient dates are written anywhere. Standard library only.

## Install (about 10 minutes)

```bash
sudo mkdir -p /opt/fullsmile-metrics && cd /opt/fullsmile-metrics
# copy fullsmile_metrics.py and .env.example here, then:
cp .env.example fullsmile_metrics.env && chmod 600 fullsmile_metrics.env
nano fullsmile_metrics.env           # fill in the values
python3 fullsmile_metrics.py --check    # OpenDental, GHL and GitHub must all say OK
python3 fullsmile_metrics.py --dry-run  # builds ./practice.json without pushing; open it and check the numbers
python3 fullsmile_metrics.py            # first real push
```

Schedule it every 2 hours, 7 AM to 7 PM Central (the droplet clock is UTC):

```bash
crontab -e
# add:
0 12-23/2,0 * * * cd /opt/fullsmile-metrics && /usr/bin/python3 fullsmile_metrics.py >> /var/log/fullsmile-metrics.log 2>&1
```

It only commits when the numbers change, and each commit rebuilds the dashboard.

## What it needs

- **Open Dental API**: the same developer key and customer key the sync uses. The key's permissions in Open Dental must allow **Queries** (ShortQuery). If `--check` fails on OpenDental with 401 or 403, enable it under Setup → Advanced Setup → API.
- **GHL token** for the Full Smile sub-account: contacts.readonly, opportunities.readonly, locations/customFields.readonly.
- **GitHub fine-grained token**: repository access = only `fullsmile-dashboard`, permission Contents = Read and write.

## What it reads

| Number | From |
|---|---|
| Production | procedurelog, ProcStatus = 2, ProcFee × (UnitQty + BaseUnits), by ProcDate |
| Collected | paysplit.SplitAmt by DatePay, plus claimproc.InsPayAmt (Status Received/Supplemental) by DateCP. Write-offs excluded. |
| Visits, broken, show rate | appointment by AptDateTime, AptStatus 2 = complete, 5 = broken |
| New patients | patient.DateFirstVisit, counted by week |
| Source of a new patient | GHL contact matched by PatNum field, then phone, then email; else the OpenDental "Referred from" entry |
| Services | CDT code ranges (D6000s implants, D2700s crowns, D9947–D9949 sleep apnea, D7000s surgery, etc.) |
| Snapshots | recall overdue, planned treatment with no appointment, broken appointments not rebooked, active patients |

Leads: GHL contacts, excluding contacts created by the OpenDental sync (source "OpenDental", "OD Patient", or tagged
`open-dental-synced` with no marketing source) and anything tagged spam/test/duplicate.

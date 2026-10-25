# Earthrise Water Intelligence — Public Deployment

## Overview
Public visitors can view **verified** historical IID water-quality charts and Lake Mead analyses. Only an administrator can import PDFs/CSV, review values, refresh Lake Mead, or access full archives. Uploaded PDFs and SQLite live on a Render persistent disk (`/var/data`).

## Deploy on Render (paid web service + disk)
1. Create a GitHub **private** repository. Upload the contents of this folder (not the parent ZIP). Never upload private PDFs, `data/`, a SQLite database, or passwords to GitHub. The two included CSV files are **reference data bundled with the original app**, and are **not auto-imported**; remove them from the repository if you don't want these data stored there.
2. Log into https://dashboard.render.com and choose **New > Blueprint**. Connect the GitHub repository; Render reads `render.yaml`.
3. Select the paid Starter web service and confirm the 5 GB disk with mount path `/var/data`. Check current plan/disk pricing before confirming.
4. Set `EARTHRISE_ADMIN_PASSWORD` as a strong secret (at least 20 random characters) in Render's environment variables. **Never commit it to Git.**
5. Deploy. Open your new HTTPS `https://<your-app>.onrender.com` URL. Public dashboards are read-only.
6. In the sidebar open **Administrator access**, enter your password, then go to **Import PDFs**. Upload IID reports or `historical_preliminary_2010_2025.csv`, then use **Review & validate** to confirm measurements before they are published to all viewers. Original reports are preserved on disk.
7. On **Lake Mead**, administrator clicks **Refresh official Lake Mead records**. The app ships with a historical seed for immediate initial charts.

## Existing local database migration
Do NOT upload `data/` to GitHub. To migrate existing local history, import the bundled/exported CSV using the admin Import page; this requires re-reviewing records. To preserve old review decisions and original PDFs, arrange a one-time secure migration of `earthrise.sqlite3` **and its WAL/SHM files where present** plus the `pdfs` folder to `/var/data`, with the app stopped. Prefer a SQLite `.backup` snapshot instead of copying an open database. Paths to uploaded originals are resolved by SHA-256 filename under `/var/data/pdfs` in this version. Make a backup before migration.

## Persistent storage and backup
Render retains files only within the mounted `/var/data` directory. Render disks use snapshots, but download your own backups routinely using the admin **Data vault** and separately back up original PDFs/SQLite via a secure storage migration or host access mechanism. The admin ZIP export contains CSVs but not every PDF and not a SQLite snapshot. A persistent disk isn't a substitute for off-site backups.

## Running locally
Install Docker Desktop, then from the project directory:
```bash
docker build -t earthrise-water .
docker run --rm -p 10000:10000 -e EARTHRISE_ADMIN_PASSWORD=replace-with-a-long-secret -v earthrise_data:/var/data earthrise-water
```
Open http://localhost:10000. For Windows, PowerShell is suitable. Alternatively install `requirements.txt` and run `streamlit run app.py` directly, setting `EARTHRISE_DATA_DIR` if needed.

## Security and operational limitations
- This is a **public demonstration/early production baseline**, not an independently security-audited public service. The password gate is **app-level session protection**, not identity-provider SSO, multi-factor authentication, or robust brute-force protection. For a public deployment, place uploads/admin pages behind Cloudflare Access or a comparable access layer, ideally with MFA, or move to a proper identity provider.
- Do not store confidential documents in a public environment until controls have been validated. Public viewers see only records with `verified` status; admins may view preliminary data. A reviewer should check station identity, units, sample date, and censored results prior to publishing.
- No public upload, no public archive exports. Admin password must be configured; otherwise administration is disabled.
- Single Render instance with mounted disk; attached disks prevent zero-downtime deployments and horizontal scaling. SQLite is appropriate for this modest single-instance setup; more concurrent users will warrant managed Postgres + object storage.
- Lake Mead data freshness is admin-triggered, not a background scheduler.
- Streamlit session auth is shared secret only and persists within browser session. Change password promptly if it leaks.
- Do not present preliminary correlations as causal proof of RO membrane fouling.

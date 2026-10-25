# Earthrise Water Intelligence Pro — Quality & Coverage Upgrade

## New features
- Availability heatmap with three modes: all rows, verified numerical readings, and uniquely dated sampling events.
- Year/parameter/status drilldown and downloadable CSV.
- Data completeness audit with missing coverage, review needs, and missing sample dates.
- Possible duplicate diagnostic (no automatic deletion).
- Admin-only archived-PDF OCR preview and reprocessing, preserving verified/excluded records.

## Safe upgrade on Render
1. Back up the SQLite database and original PDFs from `/var/data` before upgrading.
2. Replace repository `app.py` with this version. Keep your `render.yaml`, persistent disk mount and administrator password environment variable.
3. Commit/push to the existing GitHub repository. Render should deploy the updated image.
4. Verify archived PDFs and previously verified results remain intact.
5. Open Water quality trends -> Measurement availability; expand diagnostics, select a year/parameter for drilldown.
6. If archived reports had zero candidates: sign in as administrator -> Import PDFs -> Archived PDF -> Preview OCR extraction. Review candidates before commit.

## Scientific cautions
- No reading is auto-verified.
- Sample counts are not concentrations. The absence of a result in the database is not proof IID did not measure it.
- Without unique station+sample identifiers, apparent duplicates are review flags only.
- Older scanned files may still return zero candidates: OCR recognition and extracting columns are separate problems.
- CSV historical imports are provisional and may have approximate sample dates.

"""Earthrise IID Water Quality Explorer. Run: streamlit run app.py"""
from pathlib import Path
import datetime as dt
import hashlib
import io
import os
import hmac
import re
import sqlite3
import subprocess
import tempfile
import urllib.request

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import fitz

APP = Path(__file__).resolve().parent
DATA = Path(os.environ.get('EARTHRISE_DATA_DIR', str(APP / 'data'))).resolve()
PDF_DIR = DATA / 'pdfs'
PDF_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA / 'earthrise.sqlite3'
MEAD_URL = 'https://www.usbr.gov/lc/region/g4000/hourly/mead-elv.html'
MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC']
# Strict unit preservation; aluminum ug/L converted to mg/L only when unit is identified.
PARAMS = {
 'Aluminum (Al)': 'Aluminum', 'Iron (Fe)': 'Iron', 'Turbidity':'Turbidity',
 'Specific Conductance (E.C.)':'Conductivity', 'Total Filterable Residue/TDS':'TDS',
 'Total Dissolved Solids':'TDS', 'Sulfate (SO4)':'Sulfate', 'Chloride (Cl)':'Chloride',
 'pH (Lab)':'pH', 'pH (Field)':'pH', 'Alkalinity, Total (as CaCO3)':'Alkalinity',
 'Calcium (Ca)':'Calcium','Magnesium (Mg)':'Magnesium','Sodium (Na)':'Sodium',
 'Reactive Silica':'Silica', 'Silica (SiO2)':'Silica', 'Total Suspended Solids':'TSS',
 'Apparent Color':'Color','Manganese (Mn)':'Manganese',
}
UNITS = {'Aluminum':'mg/L','Iron':'mg/L','Turbidity':'NTU','Conductivity':'uS/cm',
 'TDS':'mg/L','Sulfate':'mg/L','Chloride':'mg/L','pH':'pH','Alkalinity':'mg/L as CaCO3',
 'Calcium':'mg/L','Magnesium':'mg/L','Sodium':'mg/L','Silica':'mg/L',
 'TSS':'mg/L','Color':'color units','Manganese':'mg/L'}
RE_YEAR = re.compile(r'\b(20\d{2})\b')
RE_DATE = re.compile(r'(?:Sample Date\s*:?\s*)(\d{1,2}/\d{1,2}/\d{2,4})', re.I)
RE_EHL = re.compile(r'\bEast\s+High\s*(?:line|lane)(?:\s+Canal)?\b',re.I)
RE_OTHER = re.compile(r'^\s*(?:Westside\s+Main|Central\s+Main|AAC\s+Drop\s*4|Drop\s*4|All\s+American\s+Canal|Imperial\s+Dam)\b',re.I)
RE_SECTION = re.compile(r'^\s*(?:East\s+High\s*(?:line|lane)(?:\s+Canal)?|Westside\s+Main|Central\s+Main|AAC\s+Drop\s*4|Drop\s*4|All\s+American\s+Canal)\b',re.I)

def connect():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA journal_mode=WAL')
    return c

def setup():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
          filename TEXT NOT NULL, path TEXT NOT NULL, uploaded_at TEXT NOT NULL, extraction_note TEXT);
        CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY, document_id INTEGER,
          sample_date TEXT, year INTEGER NOT NULL, parameter TEXT NOT NULL,
          value REAL, unit TEXT, reported_result TEXT, method TEXT,
          source_page INTEGER, raw_line TEXT, status TEXT NOT NULL DEFAULT 'needs_review',
          confidence_note TEXT, UNIQUE(document_id,source_page,sample_date,parameter,raw_line),
          FOREIGN KEY(document_id) REFERENCES documents(id));
        CREATE TABLE IF NOT EXISTS mead(date TEXT PRIMARY KEY, elevation_ft REAL NOT NULL,
          retrieved_at TEXT NOT NULL, source_url TEXT NOT NULL);
        ''')

def sha(data): return hashlib.sha256(data).hexdigest()

def get_year(filename):
    m = RE_YEAR.search(filename)
    return int(m.group(1)) if m else None

def to_date(txt):
    if not txt:return None
    try:return pd.to_datetime(txt).date().isoformat()
    except Exception:return None

def normalize_analyte(name, unit):
    key = PARAMS.get(name.strip())
    if not key:return None, None, None
    cleaned = (unit or '').replace('µ','u').replace('μ','u').strip()
    if key in ('Aluminum','Iron','Manganese'):
        if cleaned.lower() in ('ug/l','mcg/l'):return key, 'mg/L', .001
        if cleaned.lower() == 'mg/l':return key,'mg/L',1.
        return None,None,None
    if key=='Conductivity' and cleaned.lower() in ('umhos/cm','us/cm','µs/cm'):
        return key, 'uS/cm', 1.
    if key=='Turbidity' and cleaned.upper()=='NTU':return key,'NTU',1.
    if key=='pH' and ('ph' in cleaned.lower() or not cleaned):return key,'pH',1.
    if key=='Alkalinity' and cleaned.lower()=='mg/l':return key,'mg/L as CaCO3',1.
    if cleaned.lower() == 'mg/l':return key,'mg/L',1.
    if key=='Color' and 'color' in cleaned.lower():return key,'color units',1.
    return None,None,None

def pdf_text_pages(pdf_bytes):
    pdf=fitz.open(stream=pdf_bytes,filetype='pdf')
    pages=[]
    for pg in pdf:
        text=pg.get_text('text',sort=True)
        # pdftotext -layout preserves IID table spacing better for text reports
        pages.append(text)
    return pages

def pdftotext_layout(pdf_bytes):
    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf') as f:
            f.write(pdf_bytes);f.flush()
            p=subprocess.run(['pdftotext','-layout',f.name,'-'],capture_output=True,timeout=40)
            if p.returncode==0:
                return p.stdout.decode('utf-8',errors='replace').split('\f')
    except (FileNotFoundError, subprocess.TimeoutExpired):pass
    return pdf_text_pages(pdf_bytes)

def parse_iid(data, filename, allow_ocr=False):
    pages=pdftotext_layout(data)
    year=get_year(filename)
    records=[]; notes=[]
    for pgno,page in enumerate(pages,1):
        if len(page.strip())<180:
            notes.append(f'Page {pgno}: scanned/low text; needs manual review or OCR.')
            if allow_ocr:
                try:
                    import pytesseract
                    from PIL import Image
                    pdf=fitz.open(stream=data,filetype='pdf')
                    pix=pdf[pgno-1].get_pixmap(matrix=fitz.Matrix(2,2))
                    page=pytesseract.image_to_string(Image.open(io.BytesIO(pix.tobytes('png'))),config='--psm 6')
                except Exception as ex:
                    notes.append(f'Page {pgno}: OCR unavailable ({str(ex)[:100]}).')
        # Never use another canal's results. Identify segment headings with water sample IDs/dates.
        lines=page.splitlines()
        blocks=[]; active=False; block=[]
        for line in lines:
            if RE_SECTION.match(line) and (re.search(r'\b(?:Water|Sample\s+Date|\d{2}[A-Z]\d{4}-\d{2})\b',line,re.I) or RE_OTHER.match(line) or RE_EHL.match(line)):
                if active and block:blocks.append('\n'.join(block))
                active=bool(RE_EHL.match(line));block=[line] if active else []
            elif active:block.append(line)
        if active and block:blocks.append('\n'.join(block))
        if not blocks:
            # For an entire East Highline-only PDF, include only when its first page clearly identifies station;
            # subsequent pages without station headings remain excluded for safety.
            notes.append(f'Page {pgno}: no safely isolated East Highline sample block.')
            continue
        for block in blocks:
            dm=RE_DATE.search(block)
            date=to_date(dm.group(1)) if dm else None
            yr=int(date[:4]) if date else year
            if not yr:notes.append(f'Page {pgno}: unknown year');continue
            for raw in block.splitlines():
                fields=re.split(r'\s{2,}',raw.strip())
                # Expected: Analyte | Method | Result | Reporting Limit | MCL | Units | ...
                # Some reports have no MCL; locate units among columns after result.
                if len(fields)<4:continue
                name=fields[0].strip()
                if name not in PARAMS:continue
                method=fields[1].strip()
                valstr=fields[2].strip()
                if not re.fullmatch(r'(?:ND|N/D|<\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?)',valstr,re.I):continue
                unit=''
                for f in fields[3:8]:
                    if re.fullmatch(r'(?:ug/L|µg/L|mg/L|NTU|umhos/cm|uS/cm|µS/cm|pH Units|Color Units|pH)',f.strip(),re.I):
                        unit=f.strip();break
                key,unit_normalized,factor=normalize_analyte(name,unit)
                if not key:continue
                nondetect=valstr.upper() in ('ND','N/D') or valstr.startswith('<')
                value=None if nondetect else float(valstr)*factor
                records.append(dict(sample_date=date,year=yr,parameter=key,value=value,
                    unit=unit_normalized,reported_result=valstr,method=method,
                    source_page=pgno,raw_line=raw.strip(),status='needs_review',
                    confidence_note=('nondetect/censored; do not treat as zero' if nondetect else
                                     'OCR-derived; verify against PDF' if len(pages[pgno-1].strip())<180 else
                                     'PDF text-derived; verify against PDF')))
    if not records:notes.append('No safely parsed table rows. Upload a reviewed CSV or enter results in the editor.')
    return records, '\n'.join(dict.fromkeys(notes))

def import_csv_frame(frame, source_label='historical CSV import'):
    required={'year','parameter','value'}
    if not required.issubset(frame.columns):
        raise ValueError('CSV must contain year, parameter, value columns.')
    count=0
    with connect() as c:
        for _,r in frame.iterrows():
            try:year=int(r['year'])
            except Exception:continue
            par=str(r['parameter']).strip()
            if not par or par.lower()=='nan':continue
            value=pd.to_numeric(r['value'],errors='coerce')
            val=None if pd.isna(value) else float(value)
            date=None if pd.isna(r.get('sample_date')) else str(r.get('sample_date'))
            unit=str(r.get('unit',''))
            label=str(r.get('source_pdf',source_label))
            raw=str(r.get('raw_line','Imported CSV row'))
            exists=c.execute("""SELECT id FROM measurements WHERE document_id IS NULL
                       AND year=? AND parameter=? AND COALESCE(sample_date,'')=COALESCE(?,'')
                       AND COALESCE(value,-99999)=COALESCE(?,-99999) AND raw_line=?""",
                       (year,par,date,val,raw)).fetchone()
            if exists:continue
            c.execute("""INSERT INTO measurements(document_id,sample_date,year,parameter,value,unit,
                   reported_result,method,source_page,raw_line,status,confidence_note)
                   VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?)""",
                   (date,year,par,val,unit,str(r.get('reported_result','')),source_label,None,raw,
                    'needs_review',f'Imported from {label}; confirm sample station/date/units against source'))
            count+=1
    return count

def store_pdf(name, b, ocr=False):
    digest=sha(b)
    with connect() as c:
        found=c.execute('SELECT id FROM documents WHERE sha256=?',(digest,)).fetchone()
        if found:return 0,0,True
        target=PDF_DIR/(digest+'.pdf')
        target.write_bytes(b)
        rows, note=parse_iid(b,name,ocr)
        cur=c.execute('INSERT INTO documents(sha256,filename,path,uploaded_at,extraction_note) VALUES (?,?,?,?,?)',
          (digest,name,str(target),dt.datetime.now().isoformat(timespec='seconds'),note))
        id=cur.lastrowid
        for r in rows:
            c.execute('''INSERT OR IGNORE INTO measurements(document_id,sample_date,year,parameter,value,unit,reported_result,method,source_page,raw_line,status,confidence_note)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(id,*[r[x] for x in ['sample_date','year','parameter','value','unit','reported_result','method','source_page','raw_line','status','confidence_note']]))
        return id,len(rows),False

def seed_mead_once():
    seed=APP/'lake_mead_historical_seed.csv'
    if not seed.exists():return
    with connect() as c:
        n=c.execute('SELECT COUNT(*) FROM mead').fetchone()[0]
        if n:return
        frame=pd.read_csv(seed)
        for _,r in frame.iterrows():
            c.execute('INSERT OR IGNORE INTO mead(date,elevation_ft,retrieved_at,source_url) VALUES(?,?,?,?)',
              (str(r['date']),float(r['mead_elevation_ft']),'Bundled historical snapshot',MEAD_URL))

def mead_refresh():
    req=urllib.request.Request(MEAD_URL,headers={'User-Agent':'EarthriseResearchDashboard/1.0'})
    with urllib.request.urlopen(req,timeout=35) as response:html=response.read()
    tables=pd.read_html(io.BytesIO(html))
    data=[]
    for table in tables:
        df=table.copy();df.columns=[str(c).upper().strip() for c in df.columns]
        year_col=next((x for x in df.columns if x=='YEAR'),None)
        if not year_col:continue
        if not sum(x in df.columns for x in MONTHS)>=10:continue
        for _,r in df.iterrows():
            try: year=int(r[year_col])
            except Exception:continue
            if not 1935<=year<=dt.datetime.now().year:continue
            for month_idx,m in enumerate(MONTHS,1):
                if m not in df:continue
                try:elev=float(str(r[m]).replace(',',''))
                except Exception:continue
                if not 600<=elev<=1300:continue
                date=pd.Timestamp(year=year,month=month_idx,day=1)+pd.offsets.MonthEnd(0)
                data.append((date.date().isoformat(),elev))
    if not data:raise ValueError('USBR table format changed; no valid water elevations found.')
    with connect() as c:
        for day,elev in data:
            c.execute('''INSERT INTO mead(date,elevation_ft,retrieved_at,source_url) VALUES (?,?,?,?)
                 ON CONFLICT(date) DO UPDATE SET elevation_ft=excluded.elevation_ft,
                 retrieved_at=excluded.retrieved_at,source_url=excluded.source_url''',
                 (day,elev,dt.datetime.now().isoformat(timespec='seconds'),MEAD_URL))
    return len(data)

def fetch(table):
    with connect() as c:return pd.read_sql_query('SELECT * FROM '+table,c)

def generate_annual(rows, use_approved_only=True):
    d=rows.copy()
    if use_approved_only:d=d[d.status=='verified']
    d['value']=pd.to_numeric(d.value,errors='coerce')
    d=d.dropna(subset=['value'])
    if d.empty:return pd.DataFrame(columns=['year','parameter','value','n'])
    return d.groupby(['year','parameter'],as_index=False).agg(value=('value','mean'),n=('value','size'))

def correlation_frame(annual, mead, method='annual_mean'):
    if annual.empty or mead.empty:return pd.DataFrame()
    lake=mead.copy();lake['year']=pd.to_datetime(lake.date).dt.year
    lake_year=lake.groupby('year').elevation_ft.mean().rename('lake_mead_ft').reset_index()
    wide=annual.pivot(index='year',columns='parameter',values='value').reset_index()
    return wide.merge(lake_year,on='year',how='left')

def correlation_table(wide):
    if wide.empty or 'lake_mead_ft' not in wide:return pd.DataFrame()
    out=[]
    for p in [x for x in wide.columns if x not in ('year','lake_mead_ft')]:
        for mode in ['levels','consecutive_year_changes']:
            sub=wide[['year',p,'lake_mead_ft']].dropna().sort_values('year')
            if mode=='consecutive_year_changes':
                sub=sub.assign(d_year=sub.year.diff(),**{p:sub[p].diff(),'lake_mead_ft':sub.lake_mead_ft.diff()})
                sub=sub[sub.d_year==1].dropna()
            if len(sub)<5 or sub[p].nunique()<2 or sub.lake_mead_ft.nunique()<2:continue
            from scipy.stats import pearsonr,spearmanr
            a,b=pearsonr(sub[p],sub.lake_mead_ft)
            c,d=spearmanr(sub[p],sub.lake_mead_ft)
            out.append(dict(parameter=p,method=mode,n=len(sub),pearson_r=a,pearson_p=b,spearman_r=c,spearman_p=d))
    return pd.DataFrame(out)

def zip_export(files):
    import zipfile
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for k,v in files.items():z.writestr(k,v)
    return buf.getvalue()


# ─────────────────── PRESENTATION / APP UI ───────────────────
st.set_page_config(page_title='Earthrise Engineering Center (EEC) | Water Intelligence', page_icon='🌊', layout='wide', initial_sidebar_state='expanded')
setup()
seed_mead_once()
PALETTE = ['#37C8C0','#F5B565','#6B95EC','#CD88E9','#F28583','#91B48D']
PLOT_BG = '#111E30'
FONT = '#E8F0F7'
st.markdown('''<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
.stApp {background:linear-gradient(145deg,#08121f 0%,#102237 65%,#0b1726 100%); color:#ebf3fa; font-family:'DM Sans',sans-serif;}
section[data-testid="stSidebar"] {background:#0b1929; border-right:1px solid #22384e;}
section[data-testid="stSidebar"] * {color:#cfe2ee;}
#MainMenu, footer {visibility:hidden;}
h1,h2,h3 {font-family:'Space Grotesk',sans-serif;letter-spacing:-.035em;color:#f4f9fe!important;}
h1 {font-size:2.4rem!important;} h2{font-size:1.65rem!important;}
.block-container{padding-top:2rem; max-width:1500px;}
[data-testid="stMetric"]{background:linear-gradient(145deg,#15283c,#0d1d30); border:1px solid #28405a; border-radius:16px; padding:18px 20px;}
[data-testid="stMetricLabel"]{color:#a9c3d6!important;}
[data-testid="stMetricValue"]{color:#eaf8f5!important;font-family:'Space Grotesk',sans-serif;}
[data-testid="stTabs"] button{font-weight:600;}
[data-testid="stVerticalBlockBorderWrapper"] {border-color:#284158!important; border-radius:18px!important;}
.stButton>button[kind="primary"] {background:#30c5ba;color:#06212c;border:0;font-weight:700;}
.stButton>button {border-radius:10px;}
.hero {background:radial-gradient(circle at 88% 10%,rgba(57,200,192,.24),transparent 32%),linear-gradient(120deg,#152a40,#0c2034);border:1px solid #2b465f;border-radius:20px;padding:28px 30px;margin:4px 0 24px;}
.eyebrow{color:#5ee2d6;text-transform:uppercase;letter-spacing:.2em;font-size:.72rem;font-weight:700;}
.hero-title{font:700 2.05rem 'Space Grotesk',sans-serif;color:#f1f8fb;margin:8px 0;}
.hero-desc{color:#acc9d8;font-size:.94rem;}
.note {color:#a3bbcd;font-size:.82rem;}
</style>''',unsafe_allow_html=True)

def polish(fig, height=400, title=None):
    fig.update_layout(
        template='plotly_dark', paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor=PLOT_BG,
        font=dict(family='DM Sans, sans-serif',color=FONT,size=12),
        height=height, margin=dict(l=22,r=18,t=60 if title else 35,b=40),
        colorway=PALETTE, title=dict(text=title or '',font=dict(size=17,color='#eef7ff')),
        legend=dict(bgcolor='rgba(0,0,0,0)',orientation='h',y=-.2),
        hoverlabel=dict(bgcolor='#1a3148',font_color='#fff'),
        xaxis=dict(gridcolor='#263b51',zerolinecolor='#263b51'),
        yaxis=dict(gridcolor='#263b51',zerolinecolor='#263b51'))
    return fig

def draw(fig,key=None):st.plotly_chart(fig,use_container_width=True,key=key)

def hero(kicker,title,desc):
    st.markdown(f'<div class="hero"><div class="eyebrow">{kicker}</div><div class="hero-title">{title}</div><div class="hero-desc">{desc}</div></div>',unsafe_allow_html=True)

def reviewed_data(rows, provisional=False):
    d=rows.copy()
    if not provisional:d=d[d.status=='verified']
    d['value']=pd.to_numeric(d.value,errors='coerce')
    return d.dropna(subset=['value'])

def wide_data(annual, lake):
    w=annual.pivot(index='year',columns='parameter',values='value').reset_index()
    if not lake.empty:
        w=correlation_frame(annual,lake)
    return w

# Public visitors can explore verified results; all write/administrative actions are protected.
# Administrator secret must be configured in hosting environment, never in Git.
ADMIN_PASSWORD = os.environ.get('EARTHRISE_ADMIN_PASSWORD', '')
if 'admin_ok' not in st.session_state:
    st.session_state.admin_ok = False

with st.sidebar:
    with st.expander('Administrator access', expanded=False):
        if st.session_state.admin_ok:
            st.success('Administrator session active')
            if st.button('Sign out'):
                st.session_state.admin_ok = False
                st.rerun()
        elif not ADMIN_PASSWORD:
            st.caption('Administration disabled until EARTHRISE_ADMIN_PASSWORD is configured.')
        else:
            with st.form('admin_login', clear_on_submit=True):
                entered = st.text_input('Administrator password', type='password')
                submit = st.form_submit_button('Unlock administration')
            if submit:
                if hmac.compare_digest(entered.encode('utf-8'), ADMIN_PASSWORD.encode('utf-8')):
                    st.session_state.admin_ok = True
                    st.rerun()
                else:
                    st.error('Incorrect password')
    st.markdown('### 🌊 EARTHRISE')
    st.caption('WATER INTELLIGENCE / ANALYTICS')
    pages = ['Overview','Water quality trends','Relationships & 3D','Lake Mead']
    if st.session_state.admin_ok:
        pages += ['Import PDFs','Review & validate','Data vault']
    page=st.radio('NAVIGATE', pages, label_visibility='collapsed')
    st.divider()
    show_preliminary=st.toggle('Include unverified records',value=False,disabled=not st.session_state.admin_ok,help='Only administrators may view preliminary/unverified measurements.')
    st.caption('Verified data are used by default. Unverified data are for exploration only.')
    st.divider()
    st.caption('Historical database: persistent storage enabled')
    st.caption('Lake Mead: U.S. Bureau of Reclamation')

rows=fetch('measurements');docs=fetch('documents');lake=fetch('mead')
if not st.session_state.admin_ok:
    rows=rows[rows.status=='verified'].copy() if not rows.empty else rows
    docs=docs.iloc[0:0].copy() # Do not expose internal filenames/metadata to visitors
if not lake.empty:lake['date']=pd.to_datetime(lake.date,errors='coerce')
verified_count=int((rows.status=='verified').sum()) if not rows.empty else 0
selected=reviewed_data(rows,show_preliminary) if not rows.empty else pd.DataFrame()
annual=generate_annual(rows,not show_preliminary) if not rows.empty else pd.DataFrame(columns=['year','parameter','value','n'])
if not selected.empty and 'year' in selected: selected['year']=pd.to_numeric(selected.year,errors='coerce')

if page=='Overview':
    hero('RESEARCH DASHBOARD','Water quality, clearly connected.','IID East Highline Canal • 2010–2025 historical context • Lake Mead reservoir intelligence')
    a,b,c,d=st.columns(4)
    a.metric('Stored IID PDFs',len(docs));b.metric('Measurements',len(rows));c.metric('Verified',verified_count)
    d.metric('Lake Mead months',len(lake))
    if show_preliminary:st.warning('Exploratory mode is ON — plots may include unverified PDF/OCR data.')
    if annual.empty:
        st.info('No approved numerical values yet. Import the supplied historical CSV on the Import PDFs page, review entries, or enable exploratory mode in the sidebar.')
    else:
        priority=['Aluminum','Turbidity','TDS','Conductivity']
        cols=st.columns(2)
        for i,p in enumerate(priority):
            d=annual[annual.parameter==p].sort_values('year')
            with cols[i%2]:
                if len(d):
                    fig=px.line(d,x='year',y='value',markers=True,color_discrete_sequence=[PALETTE[i]],hover_data=['n'])
                    fig.update_traces(line=dict(width=3),marker=dict(size=8,line=dict(color='#0b1b2a',width=1)))
                    fig.update_yaxes(title=f'{p} ({UNITS.get(p,"reported units")})')
                    draw(polish(fig,325,f'{p} · annual sample summary'),key=f'overview_{p}')
        if not lake.empty:
            st.subheader('Reservoir context')
            recent=lake[lake.date.dt.year>=2010].sort_values('date')
            fig=px.area(recent,x='date',y='elevation_ft',color_discrete_sequence=['#4CBBDB'])
            fig.update_traces(fillcolor='rgba(55,200,192,.15)',line=dict(width=2.5))
            fig.update_yaxes(title='Lake elevation (ft)')
            draw(polish(fig,350,'Lake Mead · monthly water elevation'))
    st.caption('Annual points are means of available observations, not necessarily representative of the whole year. Missing years are not interpolated.')

elif page=='Water quality trends':
    hero('TIME SERIES','Historical chemistry explorer','Explore individual parameters, compare year-to-year movement, and assess data coverage.')
    if annual.empty:st.info('No numeric measurements available under the selected review filter.')
    else:
        options=sorted(annual.parameter.unique());prior=[p for p in ['Aluminum','Turbidity','TDS','Conductivity'] if p in options]
        ps=st.multiselect('Parameters to visualize',options,default=prior or options[:3]); mode=st.segmented_control('View',['Individual panels','Normalized comparison'],default='Individual panels')
        if ps:
            if mode=='Individual panels':
                cols=st.columns(2)
                for i,p in enumerate(ps):
                    d=annual[annual.parameter==p].sort_values('year')
                    fig=px.line(d,x='year',y='value',markers=True,color_discrete_sequence=[PALETTE[i%len(PALETTE)]],hover_data=['n'])
                    fig.update_traces(line=dict(width=3),marker_size=9)
                    fig.update_yaxes(title=UNITS.get(p,'reported units'))
                    with cols[i%2]:draw(polish(fig,360,f'{p} · {d.year.min()}–{d.year.max()}'),key=f'trend_{p}')
            else:
                sub=annual[annual.parameter.isin(ps)].copy()
                sub['indexed']=sub.groupby('parameter').value.transform(lambda s: 100*s/s.iloc[0] if s.iloc[0]!=0 else np.nan)
                fig=px.line(sub,x='year',y='indexed',color='parameter',markers=True,color_discrete_sequence=PALETTE)
                fig.update_yaxes(title='First available measurement = 100')
                draw(polish(fig,510,'Indexed historical comparison'))
                st.caption('Indexed comparisons describe relative movement, not differences in absolute concentration.')
        count=annual.pivot_table(index='parameter',columns='year',values='n',aggfunc='sum').fillna(0)
        if len(count):
            st.subheader('Measurement availability')
            fig=px.imshow(count,aspect='auto',color_continuous_scale=[[0,'#15273a'],[0.15,'#207e88'],[1,'#53d8c4']],labels={'color':'Sample count'})
            draw(polish(fig,max(340,30*len(count)),'Coverage by parameter and year'))

elif page=='Relationships & 3D':
    hero('MULTIVARIATE ANALYSIS','Discover relationships in the data','Interactive 2D scatter, correlation matrix, and 3D chemistry–reservoir exploration.')
    if annual.empty:st.info('Import or review measurements first.')
    else:
        w=wide_data(annual,lake); numeric=[p for p in w.columns if p not in ['year','lake_mead_ft']]
        if len(numeric)<2:st.info('At least two measured parameters are needed.')
        else:
            col1,col2=st.columns(2)
            with col1: x=st.selectbox('X axis',numeric,index=numeric.index('Turbidity') if 'Turbidity' in numeric else 0)
            with col2: y=st.selectbox('Y axis',numeric,index=numeric.index('Aluminum') if 'Aluminum' in numeric else min(1,len(numeric)-1))
            paired=w[['year',x,y]].dropna() if x!=y else w[['year',x]].dropna()
            if x==y:st.warning('Choose two different parameters.')
            elif len(paired)>=2:
                fig=px.scatter(paired,x=x,y=y,hover_data=['year'],color='year',color_continuous_scale='Tealgrn',size_max=16)
                fig.update_traces(marker=dict(size=13,line=dict(color='#e9f9fa',width=1)))
                draw(polish(fig,430,f'{x} × {y} · {len(paired)} paired years'))
                if len(paired)>=5:
                    from scipy.stats import pearsonr, spearmanr
                    if paired[x].nunique()>1 and paired[y].nunique()>1:
                        r,p=pearsonr(paired[x],paired[y]);s,sp=spearmanr(paired[x],paired[y]);a,b=st.columns(2)
                        a.metric('Pearson r',f'{r:+.3f}');b.metric('Spearman ρ',f'{s:+.3f}')
                        st.caption(f'Pearson p={p:.4g}; Spearman p={sp:.4g}; n={len(paired)}. Exploratory association, not causal evidence.')
            st.subheader('3D relationship explorer')
            choices=[p for p in numeric if p not in (x,y)]
            if 'lake_mead_ft' in w and w.lake_mead_ft.notna().sum():choices=['lake_mead_ft']+choices
            if not choices or x==y:st.info('Choose different X/Y parameters and add Lake Mead data or a third parameter for 3D.')
            else:
                z=st.selectbox('3D Z axis',choices)
                tri=w[['year',x,y,z]].dropna()
                st.caption(f'{len(tri)} complete observations across all three axes. 3D axes are annual paired values.')
                if len(tri)>=3:
                    fig=px.scatter_3d(tri,x=x,y=y,z=z,color='year',hover_data=['year'],color_continuous_scale='Turbo',opacity=.88)
                    fig.update_traces(marker=dict(size=7,line=dict(width=.5,color='#F0FAFA')))
                    fig.update_layout(scene=dict(bgcolor=PLOT_BG,xaxis=dict(backgroundcolor='#14263a',gridcolor='#41536a'),yaxis=dict(backgroundcolor='#14263a',gridcolor='#41536a'),zaxis=dict(backgroundcolor='#14263a',gridcolor='#41536a'),aspectmode='cube'))
                    draw(polish(fig,660,f'3D · {x} / {y} / {z}'))
                    st.caption('Rotate with your mouse, scroll to zoom, and hover for the year. This reveals patterns but should be paired with 2D statistics.')
                else:st.warning('Not enough years have all three measures. Choose other variables or add more IID reports.')
            st.subheader('Pairwise chemistry matrix')
            valid=[p for p in numeric if w[p].notna().sum()>=3]
            if len(valid)>=2:
                corr=w[valid].corr(min_periods=3)
                fig=px.imshow(corr,zmin=-1,zmax=1,color_continuous_scale='RdBu_r',text_auto='.2f',aspect='auto')
                draw(polish(fig,max(420,35*len(valid)),'Pearson correlations between measured parameters'))
                st.caption('Pairwise correlations can use different years and small samples; check the underlying data before interpretation.')

elif page=='Lake Mead':
    hero('RESERVOIR MONITOR','Lake Mead reference series','U.S. Bureau of Reclamation month-end elevations and annual comparisons.')
    if st.session_state.admin_ok and st.button('↻ Refresh official Lake Mead records',type='primary'):
        try:
            n=mead_refresh();st.success(f'Refreshed {n} monthly records.');st.rerun()
        except Exception as ex:st.error(f'Unable to refresh ({ex}). Existing historical records were preserved.')
    if lake.empty:st.info('No Lake Mead data yet. Administrator can refresh when connected to the internet.')
    else:
        years=lake.date.dt.year; yy=st.slider('Display period',int(years.min()),int(years.max()),(max(int(years.min()),2010),int(years.max())))
        s=lake[years.between(*yy)].sort_values('date')
        fig=px.area(s,x='date',y='elevation_ft',color_discrete_sequence=['#37c8c0'])
        fig.update_traces(fillcolor='rgba(55,200,192,.18)',line=dict(width=2.6))
        draw(polish(fig,450,'Lake Mead end-of-month elevation (ft)'))
        if not annual.empty:
            wide=correlation_frame(annual,lake)
            par=st.selectbox('Compare reservoir elevation with',sorted(annual.parameter.unique()))
            match=wide[['year',par,'lake_mead_ft']].dropna()
            if len(match)>=3:
                fig=px.scatter(match,x='lake_mead_ft',y=par,color='year',hover_data=['year'],color_continuous_scale='Turbo')
                fig.update_traces(marker=dict(size=13))
                draw(polish(fig,420,f'{par} vs annual mean lake elevation'))
                if len(match)>=5:
                    from scipy.stats import pearsonr,spearmanr
                    if match[par].nunique()>1 and match.lake_mead_ft.nunique()>1:
                        r,p=pearsonr(match[par],match.lake_mead_ft);st.caption(f'Pearson r={r:+.3f}, p={p:.3g}, paired n={len(match)}. Does not imply causation.')
        st.download_button('Download Lake Mead CSV',lake.to_csv(index=False),'lake_mead_monthly.csv',mime='text/csv')

elif page=='Import PDFs' and st.session_state.admin_ok:
    hero('DATA INTAKE','Build your permanent historical archive','Import new reports once. Existing documents are detected by file hash and retained in SQLite.')
    files=st.file_uploader('Drop IID laboratory report PDFs here',type=['pdf'],accept_multiple_files=True)
    ocr=st.checkbox('Attempt OCR for scanned PDF pages (requires Tesseract + pytesseract)',value=False)
    if files and st.button('Import selected PDFs',type='primary'):
        for f in files:
            try:
                i,n,dupe=store_pdf(f.name,f.getvalue(),ocr)
                st.success(f'{f.name}: '+('Already archived' if dupe else f'Archived with {n} candidate readings'))
            except Exception as ex:st.error(f'{f.name}: {ex}')
        st.rerun()
    st.info('Scanned and multi-canal reports need human verification. Extraction attempts never automatically mark values as verified.')
    st.subheader('Historical CSV import')
    sample=st.file_uploader('Import previously extracted measurements',type=['csv'],key='csv_upload')
    if sample is not None and st.button('Import CSV',type='primary'):
        try:n=import_csv_frame(pd.read_csv(sample));st.success(f'Imported {n} new candidate rows.');st.rerun()
        except Exception as ex:st.error(str(ex))
    if not docs.empty:
        st.subheader('Archive index')
        st.dataframe(docs[['id','filename','uploaded_at','extraction_note']],hide_index=True,use_container_width=True)

elif page=='Review & validate' and st.session_state.admin_ok:
    hero('QUALITY ASSURANCE','Review extracted measurements','Approve only trustworthy values, retain censored measurements, and exclude extraction artifacts.')
    if rows.empty:st.info('No measurements stored yet.')
    else:
        years=sorted(rows.year.dropna().astype(int).unique())
        filt=st.multiselect('Years to review',years,default=years)
        sub=rows[rows.year.isin(filt)].copy()
        cols=['id','year','sample_date','parameter','value','unit','reported_result','status','source_page','document_id','confidence_note','raw_line']
        editor=st.data_editor(sub[cols],hide_index=True,num_rows='fixed',use_container_width=True,
            disabled=['id','document_id','source_page','reported_result','raw_line','confidence_note'],
            column_config={'status':st.column_config.SelectboxColumn(options=['needs_review','verified','excluded'],required=True)},key='review_pro')
        if st.button('Save review decisions',type='primary'):
            with connect() as c:
                for _,r in editor.iterrows():
                    original=sub[sub.id==r.id].iloc[0]
                    if all(str(r[k])==str(original[k]) for k in ['year','sample_date','parameter','value','unit','status']):continue
                    val=None if pd.isna(r.value) else float(r.value)
                    date=None if pd.isna(r.sample_date) or not str(r.sample_date).strip() else str(r.sample_date)
                    if date and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):st.error(f'Invalid ISO date row {r.id}');st.stop()
                    c.execute('UPDATE measurements SET year=?,sample_date=?,parameter=?,value=?,unit=?,status=? WHERE id=?',(int(r.year),date,str(r.parameter),val,str(r.unit),str(r.status),int(r.id)))
            st.success('Review decisions persisted.');st.rerun()
        st.caption('Nondetect values should retain blank numeric concentrations; do not substitute zero.')

elif page=='Data vault' and st.session_state.admin_ok:
    hero('GOVERNANCE','Data archive & portability','Export the records and original PDFs. Back up the complete data directory to preserve the database.')
    st.code(str(DB))
    payload={'measurements.csv':rows.to_csv(index=False),'documents.csv':docs.to_csv(index=False),'lake_mead_monthly.csv':lake.to_csv(index=False)}
    st.download_button('Download structured archive (ZIP)',zip_export(payload),'earthrise_data_export.zip',mime='application/zip')
    if not docs.empty:
        options={f"{r['id']} · {r['filename']}":int(r['id']) for _,r in docs.iterrows()}
        label=st.selectbox('Select archived original document',list(options))
        r=docs[docs.id==options[label]].iloc[0]
        try:
            pdf_path = PDF_DIR / (str(r.sha256) + '.pdf')
            st.download_button('Download original PDF',pdf_path.read_bytes(),r.filename,mime='application/pdf')
        except FileNotFoundError:st.error('Original PDF missing at recorded location.')
    st.warning('Your SQLite database and uploaded PDFs are stored under data/. Never delete that folder during an upgrade.')

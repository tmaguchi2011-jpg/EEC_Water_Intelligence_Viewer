            labels={'date': 'Date', 'above_dead_pool_ft': 'Feet above dead pool'}
        )
        fig_depth.update_traces(
            fillcolor='rgba(119,167,255,0.18)',
            line=dict(width=2.8),
            hovertemplate='%{x|%b %Y}<br>%{y:.1f} ft above dead pool<extra></extra>'
        )
        fig_depth.update_yaxes(rangemode='tozero')
        draw(polish(fig_depth,450,'Lake Mead - level above 895-ft dead-pool reference'))
        st.caption(
            'Calculated as water-surface elevation minus 895 ft. '
            'This is height above an operating reference, NOT the actual depth of Lake Mead.'
        )
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
        st.markdown('#### Reprocess an archived PDF (OCR diagnostics)')
        st.caption('Reprocesses original archived bytes; preserves verified/excluded rows. Only unreviewed rows are replaced on commit.')
        options={f"{int(r.id)} · {r.filename}":int(r.id) for _,r in docs.iterrows()}
        reprocess_label=st.selectbox('Archived PDF',list(options),key='reprocess_doc')
        repro_id=options[reprocess_label]
        if st.button('Preview OCR extraction',key='preview_reprocess'):
            original=docs[docs.id==repro_id].iloc[0]
            data=(PDF_DIR/(str(original.sha256)+'.pdf')).read_bytes()
            extracted,note=parse_iid(data,str(original.filename),allow_ocr=True)
            st.session_state['reprocess_preview']={'doc_id':repro_id,'rows':extracted,'note':note}
        prev=st.session_state.get('reprocess_preview')
        if prev and prev['doc_id']==repro_id:
            st.info(f"Extracted {len(prev['rows'])} candidate rows (not yet saved)")
            if prev['note']:st.code(prev['note'])
            if prev['rows']:st.dataframe(pd.DataFrame(prev['rows']),hide_index=True,use_container_width=True)
            with st.expander('OCR troubleshooting'):
                st.markdown('If no candidates appear, OCR may have recognized characters but not the station headings or table-column layout. The app deliberately avoids inserting guessed numeric results. A manually reviewed CSV remains the fallback.')
            if st.button('Commit OCR results (replace only unreviewed rows)',key='commit_reprocess',type='primary',disabled=not bool(prev['rows'])):
                with connect() as c:
                    c.execute("DELETE FROM measurements WHERE document_id=? AND status='needs_review'",(repro_id,))
                    for r in prev['rows']:
                        c.execute("""INSERT OR IGNORE INTO measurements(document_id,sample_date,year,parameter,value,unit,reported_result,method,source_page,raw_line,status,confidence_note)
                                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(repro_id,*[r[x] for x in ['sample_date','year','parameter','value','unit','reported_result','method','source_page','raw_line','status','confidence_note']]))
                    c.execute('UPDATE documents SET extraction_note=? WHERE id=?',(prev['note'],repro_id))
                st.session_state.pop('reprocess_preview',None)
                st.success('Saved unreviewed OCR candidate rows. Verified measurements were preserved.');st.rerun()


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

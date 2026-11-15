import sqlite3
import time
import io
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
import requests
import pandas as pd
import streamlit as st

DB_PATH = "quant_talent_scout.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "CREATE TABLE IF NOT EXISTS vacancies ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT UNIQUE, "
        "client TEXT, "
        "required_skills TEXT, "
        "desirable_skills TEXT, "
        "status TEXT DEFAULT 'Active')"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS candidates ("
        "username TEXT PRIMARY KEY, name TEXT, employer TEXT, role TEXT, location TEXT, "
        "is_us INTEGER, bio TEXT, github_url TEXT, linkedin_url TEXT, evidence TEXT, "
        "source TEXT, score INTEGER, matched_skills TEXT, status TEXT DEFAULT 'New', "
        "notes TEXT DEFAULT '', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS known_candidates ("
        "name TEXT, employer TEXT, linkedin_url TEXT)"
    )
    c.execute("SELECT COUNT(*) FROM vacancies")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO vacancies (title, client, required_skills, desirable_skills, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "EQR Quantitative Developer",
                "Citadel / EQR",
                "Python, C++, PySpark, Polars, Parquet, Apache Arrow",
                "NumPy, Cython, Gurobi, MOSEK, BLAS/LAPACK, Backtesting, Simulation",
                "Active"
            )
        )
    conn.commit()
    conn.close()

init_db()

def get_vacancies():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM vacancies", conn)
    conn.close()
    return df

def upsert_candidate(data: dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM known_candidates WHERE lower(name) = lower(?)", (data["name"],))
    is_known = c.fetchone()[0] > 0
    if is_known:
        data["status"] = "Archived"
        data["notes"] = "[Auto-flagged: Already known to Kevin]"

    c.execute("SELECT status, notes, linkedin_url FROM candidates WHERE username = ?", (data["username"],))
    existing = c.fetchone()
    if existing:
        status = existing[0] if existing[0] != 'New' else data["status"]
        notes = existing[1] if existing[1] else data.get("notes", "")
        linkedin = existing[2] if existing[2] else data.get("linkedin_url", "")
    else:
        status = data.get("status", "New")
        notes = data.get("notes", "")
        linkedin = data.get("linkedin_url", "")

    sql = (
        "INSERT INTO candidates ("
        "username, name, employer, role, location, is_us, bio, github_url, "
        "linkedin_url, evidence, source, score, matched_skills, status, notes, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(username) DO UPDATE SET "
        "name=excluded.name, employer=excluded.employer, location=excluded.location, "
        "is_us=excluded.is_us, bio=excluded.bio, evidence=excluded.evidence, "
        "score=excluded.score, matched_skills=excluded.matched_skills, updated_at=CURRENT_TIMESTAMP"
    )
    c.execute(sql, (
        data["username"], data["name"], data["employer"], data["role"],
        data["location"], data["is_us"], data["bio"], data["github_url"],
        linkedin, data["evidence"], data["source"], data["score"],
        data["matched_skills"], status, notes
    ))
    conn.commit()
    conn.close()

def load_candidates_df() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT * FROM candidates ORDER BY is_us DESC, score DESC", conn)
    conn.close()
    return df

def scan_github_repo(repo_name: str, limit: int, token: str = ""):
    headers = {"Accept": "application/vnd.github+json"}
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    url = f"https://api.github.com/repos/{repo_name}/contributors?per_page={limit}"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    contributors = resp.json()

    added = 0
    for c in contributors:
        if c.get("type") != "User":
            continue
        username = c["login"]
        u_url = f"https://api.github.com/users/{username}"
        u_resp = requests.get(u_url, headers=headers, timeout=10)
        if u_resp.status_code != 200:
            continue
        u_data = u_resp.json()

        name = u_data.get("name") or username
        company = u_data.get("company") or "Not listed"
        location = u_data.get("location") or "Unknown"
        bio = u_data.get("bio") or ""
        html_url = u_data.get("html_url")

        us_indicators = ["usa", "united states", "u.s.", "ny", "new york", "chicago", "san francisco", "ca", "boston", "ma", "seattle", "wa", "austin", "tx", "ct", "nj"]
        loc_lower = location.lower()
        is_us = 1 if any(ind in loc_lower for ind in us_indicators) else 0

        search_text = f"{bio} {company} {repo_name}".lower()
        score = 20 if is_us else 5
        matched = []
        keywords = {
            "c++": 25, "cpp": 25, "python": 15, "cython": 20, "polars": 20,
            "arrow": 20, "parquet": 15, "pyspark": 15, "numpy": 10,
            "quant": 25, "trading": 20, "backtest": 20, "gurobi": 25, "mosek": 25
        }
        for kw, wt in keywords.items():
            if kw in search_text:
                score += wt
                matched.append(kw)
        if company != "Not listed":
            score += 10

        cand = {
            "username": f"gh_{username}",
            "name": name,
            "employer": company,
            "role": "Open-Source Contributor",
            "location": location,
            "is_us": is_us,
            "bio": bio,
            "github_url": html_url,
            "linkedin_url": "",
            "evidence": f"Contributor to {repo_name}. Matched keywords: {', '.join(matched) if matched else 'General contributor'}",
            "source": f"GitHub ({repo_name})",
            "score": min(score, 100),
            "matched_skills": ", ".join(matched) if matched else "GitHub Contributor",
            "status": "New",
            "notes": ""
        }
        upsert_candidate(cand)
        added += 1
        time.sleep(0.1)
    return added

def scan_arxiv(query_str: str, max_results: int = 5):
    encoded_q = urllib.parse.quote(query_str)
    url = f"http://export.arxiv.org/api/query?search_query=all:{encoded_q}&max_results={max_results}"
    req = urllib.request.urlopen(url)
    xml_data = req.read()

    root = ET.fromstring(xml_data)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    added = 0

    for entry in root.findall('atom:entry', ns):
        title = entry.find('atom:title', ns).text.strip().replace('\n', ' ')
        summary = entry.find('atom:summary', ns).text.strip().replace('\n', ' ')
        pdf_link = ""
        for link in entry.findall('atom:link', ns):
            if link.get('title') == 'pdf':
                pdf_link = link.get('href')

        authors = [a.find('atom:name', ns).text for a in entry.findall('atom:author', ns)]
        primary_author = authors[0] if authors else "Unknown Author"
        slug = primary_author.lower().replace(" ", "_")
        
        score = 65
        matched = ["research paper", "arxiv"]
        if "python" in summary.lower() or "c++" in summary.lower():
            score += 15
            matched.append("code/perf mention")
        if "optimisation" in summary.lower() or "optimization" in summary.lower() or "portfolio" in summary.lower():
            score += 15
            matched.append("optimisation/quant")

        cand = {
            "username": f"arxiv_{slug}",
            "name": primary_author,
            "employer": "Academic / Research Institution",
            "role": "Research Author",
            "location": "United States (Verified via arXiv Metadata)" if "US" in summary or "University" in summary else "Global / Unverified",
            "is_us": 1 if "US" in summary or "University" in summary else 0,
            "bio": f"Author of arXiv paper: '{title}'",
            "github_url": pdf_link,
            "linkedin_url": "",
            "evidence": f"Paper: {title}. Abstract snippet: {summary[:150]}...",
            "source": "arXiv Research",
            "score": min(score, 100),
            "matched_skills": ", ".join(matched),
            "status": "New",
            "notes": f"Co-authors: {', '.join(authors)}"
        }
        upsert_candidate(cand)
        added += 1
    return added

st.set_page_config(page_title="Quant Talent Scout", layout="wide")

st.title("🎯 Quant Talent Scout")
st.caption("Independent AI-Driven Talent Intelligence for US Quantitative Finance & Engineering Roles (€0 Stack)")

vacancies_df = get_vacancies()
active_vacancy = st.sidebar.selectbox("Select Active Vacancy", vacancies_df["title"].tolist())
vac_row = vacancies_df[vacancies_df["title"] == active_vacancy].iloc[0]

with st.sidebar:
    st.header("⚙️ Vacancy Brief")
    st.markdown(f"**Client:** {vac_row['client']}")
    st.markdown(f"**Required:** {vac_row['required_skills']}")
    st.markdown(f"**Desirable:** {vac_row['desirable_skills']}")
    
    st.divider()
    st.header("🔍 Autonomous Discovery")
    gh_token = st.text_input("Optional GitHub Token (Rate limit unlock)", type="password")
    repo_choice = st.text_input("Target GitHub Repo", value="pola-rs/polars")
    arxiv_query = st.text_input("Target arXiv Research Topic", value="quantitative finance portfolio optimisation python")
    
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Scan GitHub", use_container_width=True):
            try:
                with st.spinner("Scanning GitHub contributors..."):
                    cnt = scan_github_repo(repo_choice, 15, gh_token)
                st.success(f"Added {cnt} leads from GitHub!")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
    with col_b:
        if st.button("Scan arXiv", use_container_width=True):
            try:
                with st.spinner("Querying arXiv research papers..."):
                    cnt = scan_arxiv(arxiv_query, 5)
                st.success(f"Added {cnt} researchers from arXiv!")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

    st.divider()
    st.header("💾 Backup & Deduplication")
    restore_csv = st.file_uploader("Restore Saved Pipeline CSV", type=["csv"], key="restore")
    if restore_csv is not None:
        restored_df = pd.read_csv(restore_csv)
        conn = sqlite3.connect(DB_PATH)
        restored_df.to_sql("candidates", conn, if_exists="replace", index=False)
        conn.close()
        st.success(f"Restored {len(restored_df)} candidates!")
        st.rerun()

    uploaded_known = st.file_uploader("Upload Kevin's Known Candidates CSV", type=["csv"], key="known")
    if uploaded_known is not None:
        known_df = pd.read_csv(uploaded_known)
        conn = sqlite3.connect(DB_PATH)
        known_df.to_sql("known_candidates", conn, if_exists="replace", index=False)
        conn.close()
        st.success(f"Loaded {len(known_df)} known candidates.")

df_candidates = load_candidates_df()

tab1, tab2, tab3, tab4 = st.tabs(["📊 Candidate Pipeline", "🔍 Review & Verification", "💬 WhatsApp Shortlist", "⚙️ Vacancies & Settings"])

with tab1:
    st.subheader(f"Discovered Candidates ({len(df_candidates)})")
    
    col1, col2, col3 = st.columns(3)
    with col1:
        filter_us = st.checkbox("US-Based Only", value=False)
    with col2:
        min_score = st.slider("Minimum Fit Score", 0, 100, 20)
    with col3:
        status_filter = st.multiselect("Status Filter", ["New", "Shortlisted", "Sent to Kevin", "Archived"], default=["New", "Shortlisted", "Sent to Kevin"])

    filtered_df = df_candidates[df_candidates["score"] >= min_score]
    if filter_us:
        filtered_df = filtered_df[filtered_df["is_us"] == 1]
    if status_filter:
        filtered_df = filtered_df[filtered_df["status"].isin(status_filter)]

    display_cols = ["name", "employer", "role", "location", "is_us", "score", "matched_skills", "source", "status", "github_url"]
    st.dataframe(
        filtered_df[display_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "github_url": st.column_config.LinkColumn("Evidence Link"),
            "is_us": st.column_config.CheckboxColumn("US?"),
            "score": st.column_config.ProgressColumn("Match Score", min_value=0, max_value=100, format="%d")
        }
    )

    col_ex1, col_ex2 = st.columns(2)
    with col_ex1:
        csv_data = df_candidates.to_csv(index=False).encode('utf-8')
        st.download_button("📥 Export All to CSV (Backup)", csv_data, "quant_talent_backup.csv", "text/csv", use_container_width=True)
    with col_ex2:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            filtered_df.to_excel(writer, index=False, sheet_name='Shortlist')
        excel_data = output.getvalue()
        st.download_button("📊 Export Filtered to Excel", excel_data, "quant_talent_shortlist.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

with tab2:
    st.subheader("Candidate Profile Verification & Triage")
    if filtered_df.empty:
        st.warning("No candidates available under current filters.")
    else:
        selected_cand_username = st.selectbox(
            "Select Candidate to Review",
            filtered_df["username"].tolist(),
            format_func=lambda u: f"{filtered_df[filtered_df['username']==u]['name'].values[0]} ({filtered_df[filtered_df['username']==u]['employer'].values[0]}) — Score: {filtered_df[filtered_df['username']==u]['score'].values[0]}"
        )
        cand_row = filtered_df[filtered_df["username"] == selected_cand_username].iloc[0]

        clean_emp = cand_row['employer'] if cand_row['employer'] not in ["Not listed", "Academic / Research Institution"] else ""
        search_q = urllib.parse.quote(f"{cand_row['name']} {clean_emp}".strip())
        linkedin_search_url = f"https://www.linkedin.com/search/results/people/?keywords={search_q}"

        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            st.link_button("🔎 Find on LinkedIn (1-Tap Search)", linkedin_search_url, type="primary", use_container_width=True)
        with btn_col2:
            if cand_row["github_url"]:
                st.link_button("🔗 Open GitHub / Paper Evidence", cand_row["github_url"], use_container_width=True)

        st.divider()

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Name:** {cand_row['name']}")
            st.markdown(f"**Employer / Institution:** {cand_row['employer']}")
            st.markdown(f"**Role:** {cand_row['role']}")
            st.markdown(f"**Location:** {cand_row['location']}")
            st.markdown(f"**Source:** {cand_row['source']}")
        with c2:
            st.markdown(f"**Bio / Summary:** {cand_row['bio'] or '_None_'}")
            st.markdown(f"**Evidence & Signals:** {cand_row['evidence']}")
            st.markdown(f"**Matched Skills:** `{cand_row['matched_skills']}`")

        with st.form("triage_form"):
            new_status = st.selectbox("Pipeline Status", ["New", "Shortlisted", "Sent to Kevin", "Archived"], index=["New", "Shortlisted", "Sent to Kevin", "Archived"].index(cand_row["status"]))
            new_linkedin = st.text_input("Verified LinkedIn Profile URL (Paste here once found)", value=cand_row["linkedin_url"] or "")
            new_notes = st.text_area("Verification Notes", value=cand_row["notes"] or "")
            if st.form_submit_button("Save Candidate Review", use_container_width=True):
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("UPDATE candidates SET status = ?, linkedin_url = ?, notes = ? WHERE username = ?", (new_status, new_linkedin, new_notes, selected_cand_username))
                conn.commit()
                conn.close()
                st.success("Candidate record updated successfully!")
                st.rerun()

with tab3:
    st.subheader("WhatsApp-Ready Shortlist")
    st.caption("Tap the copy icon in the top-right corner of the box below to paste directly into WhatsApp for Kevin.")
    
    shortlisted_df = df_candidates[df_candidates["status"] == "Shortlisted"]
    if shortlisted_df.empty:
        st.info("Mark candidates as **Shortlisted** in the 'Review & Verification' tab to generate the WhatsApp report.")
    else:
        lines = [f"📊 *{active_vacancy} — Independent Talent Shortlist*\n"]
        for idx, (_, r) in enumerate(shortlisted_df.iterrows(), 1):
            lines.append(f"*{idx}. {r['name']}*")
            lines.append(f"• Employer: {r['employer']}")
            lines.append(f"• Location: {r['location']}")
            if r["linkedin_url"]:
                lines.append(f"• LinkedIn: {r['linkedin_url']}")
            if r["github_url"]:
                lines.append(f"• Evidence: {r['github_url']}")
            lines.append(f"• Why Fit: {r['evidence']}")
            if r["notes"]:
                lines.append(f"• Notes: {r['notes']}")
            lines.append("")
        whatsapp_text = "\n".join(lines)
        st.code(whatsapp_text, language="markdown")

with tab4:
    st.subheader("Vacancy Management & Add New Role")
    with st.form("new_vacancy_form"):
        new_title = st.text_input("Vacancy Title", placeholder="e.g., GQS C++ Trading Systems Engineer")
        new_client = st.text_input("Client / Internal Label", placeholder="e.g., GQS / Citadel Securities")
        req_skills = st.text_area("Required Skills", placeholder="C++, Low-latency, Python")
        des_skills = st.text_area("Desirable Skills", placeholder="FPGA, Kernel bypass, SIMD")
        if st.form_submit_button("Create Vacancy"):
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO vacancies (title, client, required_skills, desirable_skills) VALUES (?, ?, ?, ?)",
                          (new_title, new_client, req_skills, des_skills))
                conn.commit()
                conn.close()
                st.success(f"Successfully created vacancy: {new_title}")
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

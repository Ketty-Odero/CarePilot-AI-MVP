import streamlit as st
import sqlite3
from datetime import datetime, date, time as dtime, timedelta
import pandas as pd
import matplotlib.pyplot as plt
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import os

DB_PATH = "carepilot.db"

# -------------------------
# Database helpers
# -------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS care_recipient (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        relationship TEXT,
        age INTEGER,
        conditions TEXT,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS medications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_id INTEGER NOT NULL,
        med_name TEXT NOT NULL,
        dose TEXT,
        schedule TEXT,
        notes TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY(recipient_id) REFERENCES care_recipient(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS med_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        medication_id INTEGER NOT NULL,
        log_date TEXT NOT NULL,        -- YYYY-MM-DD
        status TEXT NOT NULL,          -- taken/missed
        logged_at TEXT NOT NULL,
        FOREIGN KEY(medication_id) REFERENCES medications(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_id INTEGER NOT NULL,
        appt_datetime TEXT NOT NULL,   -- ISO datetime
        provider TEXT,
        purpose TEXT,
        location TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(recipient_id) REFERENCES care_recipient(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS checkins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_id INTEGER NOT NULL,
        checkin_date TEXT NOT NULL,    -- YYYY-MM-DD
        symptoms TEXT,
        symptom_severity INTEGER,      -- 0-10
        caregiver_stress INTEGER,      -- 0-10
        free_text TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(recipient_id) REFERENCES care_recipient(id)
    )
    """)

    conn.commit()
    conn.close()

def query_df(sql, params=None):
    conn = get_conn()
    df = pd.read_sql_query(sql, conn, params=params or [])
    conn.close()
    return df

def execute(sql, params=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params or [])
    conn.commit()
    conn.close()

def reset_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

# -------------------------
# Rule-based "AI" risk flags
# -------------------------
def generate_risk_flags(recipient_id: int):
    flags = []

    # Missed meds in last 3 days
    missed = query_df("""
        SELECT ml.log_date, m.med_name
        FROM med_log ml
        JOIN medications m ON m.id = ml.medication_id
        WHERE m.recipient_id = ?
          AND ml.status = 'missed'
          AND date(ml.log_date) >= date('now','-3 day')
    """, [recipient_id])

    if not missed.empty:
        meds = ", ".join(sorted(set(missed["med_name"].tolist())))
        flags.append({
            "level": "High",
            "title": "Missed medications recently",
            "detail": f"Missed med(s) in last 3 days: {meds}."
        })

    # Symptoms trend (last 3 check-ins)
    last3 = query_df("""
        SELECT checkin_date, symptom_severity
        FROM checkins
        WHERE recipient_id = ?
        ORDER BY date(checkin_date) DESC
        LIMIT 3
    """, [recipient_id])

    if len(last3) == 3:
        sev = list(reversed(last3["symptom_severity"].tolist()))  # oldest -> newest
        if all(s is not None for s in sev):
            if sev[2] >= sev[1] >= sev[0] and (sev[2] - sev[0]) >= 3:
                flags.append({
                    "level": "High",
                    "title": "Symptoms worsening trend",
                    "detail": f"Severity rose from {sev[0]} to {sev[2]} over the last 3 check-ins."
                })
            elif sev[2] >= 7:
                flags.append({
                    "level": "Medium",
                    "title": "High symptom severity",
                    "detail": f"Latest symptom severity is {sev[2]}/10."
                })

    # Caregiver stress high in last 7 days
    stress = query_df("""
        SELECT caregiver_stress
        FROM checkins
        WHERE recipient_id = ?
          AND caregiver_stress IS NOT NULL
          AND date(checkin_date) >= date('now','-7 day')
    """, [recipient_id])

    if not stress.empty and (stress["caregiver_stress"] >= 8).any():
        flags.append({
            "level": "Medium",
            "title": "Caregiver burnout risk",
            "detail": "Stress hit 8+ in the last 7 days."
        })

    # Upcoming appointment within 48 hours
    upcoming = query_df("""
        SELECT appt_datetime, provider, purpose
        FROM appointments
        WHERE recipient_id = ?
          AND datetime(appt_datetime) >= datetime('now')
          AND datetime(appt_datetime) <= datetime('now','+2 day')
        ORDER BY datetime(appt_datetime) ASC
        LIMIT 1
    """, [recipient_id])

    if not upcoming.empty:
        ap = upcoming.iloc[0]
        flags.append({
            "level": "Low",
            "title": "Upcoming appointment soon",
            "detail": f"{ap['appt_datetime']} — {ap.get('provider','')} ({ap.get('purpose','')})"
        })

    return flags

def next_step_suggestions(flags):
    titles = [f["title"] for f in flags]
    levels = [f["level"] for f in flags]
    suggestions = []

    if "High" in levels and any("Missed medications" in t for t in titles):
        suggestions.append("Review medication schedule and set a double reminder (alarm + checklist).")
        suggestions.append("Ask a family member to confirm meds for the next 2–3 days.")

    if "High" in levels and any("Symptoms worsening trend" in t for t in titles):
        suggestions.append("Contact the provider/clinic and describe the symptom trend using your logs.")
        suggestions.append("Write down: when symptoms started, what changed, and what helps.")

    if any("Caregiver burnout risk" in t for t in titles):
        suggestions.append("Delegate one task this week and schedule a daily recovery break.")

    if any("Upcoming appointment soon" in t for t in titles):
        suggestions.append("Prepare 3 questions and bring a med list + symptom timeline.")

    if not suggestions:
        suggestions.append("No urgent risks flagged. Keep logging check-ins to improve trend detection.")

    return suggestions

# -------------------------
# AI Summary (Mock AI)
# -------------------------
def build_ai_summary(recipient_id: int):
    # Med adherence (7 days)
    med_stats = query_df("""
        SELECT ml.status, COUNT(*) AS cnt
        FROM med_log ml
        JOIN medications m ON m.id = ml.medication_id
        WHERE m.recipient_id = ?
          AND date(ml.log_date) >= date('now','-7 day')
        GROUP BY ml.status
    """, [recipient_id])

    taken = missed = 0
    if not med_stats.empty:
        for _, r in med_stats.iterrows():
            if r["status"] == "taken":
                taken = int(r["cnt"])
            if r["status"] == "missed":
                missed = int(r["cnt"])
    total_logs = taken + missed
    adherence_rate = round((taken / total_logs) * 100) if total_logs > 0 else None

    # Check-ins (7 days)
    checks = query_df("""
        SELECT checkin_date, symptoms, symptom_severity, caregiver_stress, free_text
        FROM checkins
        WHERE recipient_id = ?
          AND date(checkin_date) >= date('now','-7 day')
        ORDER BY date(checkin_date) ASC
    """, [recipient_id])

    earliest_sev = latest_sev = None
    max_stress = None
    symptom_keywords = []

    if not checks.empty:
        earliest_sev = int(checks.iloc[0]["symptom_severity"]) if pd.notna(checks.iloc[0]["symptom_severity"]) else None
        latest_sev = int(checks.iloc[-1]["symptom_severity"]) if pd.notna(checks.iloc[-1]["symptom_severity"]) else None
        if checks["caregiver_stress"].notna().any():
            max_stress = int(checks["caregiver_stress"].max())

        for s in checks["symptoms"].dropna().tolist():
            parts = [p.strip().lower() for p in s.replace(";", ",").split(",")]
            symptom_keywords.extend([p for p in parts if p])
        symptom_keywords = sorted(set(symptom_keywords))[:6]

    # Upcoming appointment (14 days)
    up = query_df("""
        SELECT appt_datetime, provider, purpose
        FROM appointments
        WHERE recipient_id = ?
          AND datetime(appt_datetime) >= datetime('now')
          AND datetime(appt_datetime) <= datetime('now','+14 day')
        ORDER BY datetime(appt_datetime) ASC
        LIMIT 1
    """, [recipient_id])
    upcoming_text = None
    if not up.empty:
        ap = up.iloc[0]
        upcoming_text = f"{ap['appt_datetime']} — {ap.get('provider','')} ({ap.get('purpose','')})"

    # Risk flags
    flags = generate_risk_flags(recipient_id)
    levels = [f["level"] for f in flags]
    status = "Stable"
    if "High" in levels:
        status = "Needs Attention"
    elif "Medium" in levels:
        status = "Watch"

    # Compose summary
    summary_lines = [f"**Overall status:** {status}"]

    if adherence_rate is None:
        summary_lines.append("**Medication adherence:** No logs in the last 7 days.")
    else:
        summary_lines.append(f"**Medication adherence:** {adherence_rate}% (Taken: {taken}, Missed: {missed}) over the last 7 days.")

    if latest_sev is None:
        summary_lines.append("**Symptoms:** No check-ins in the last 7 days.")
    else:
        trend = "stable"
        if earliest_sev is not None and (latest_sev - earliest_sev) >= 3:
            trend = "worsening"
        elif earliest_sev is not None and (latest_sev - earliest_sev) <= -3:
            trend = "improving"
        summary_lines.append(f"**Symptoms:** Latest severity **{latest_sev}/10**; trend looks **{trend}** this week.")
        if symptom_keywords:
            summary_lines.append(f"**Common symptoms:** {', '.join(symptom_keywords)}")

    if max_stress is None:
        summary_lines.append("**Caregiver stress:** No stress check-ins in the last 7 days.")
    else:
        label = "manageable"
        if max_stress >= 8:
            label = "high"
        elif max_stress >= 6:
            label = "elevated"
        summary_lines.append(f"**Caregiver stress:** Peak stress **{max_stress}/10** ({label}).")

    if upcoming_text:
        summary_lines.append(f"**Upcoming appointment:** {upcoming_text}")
    else:
        summary_lines.append("**Upcoming appointment:** None in next 14 days.")

    # Actions
    actions = []
    if missed > 0:
        actions.append("Tighten medication reminders (alarm + checklist) and confirm schedule.")
    if latest_sev is not None and latest_sev >= 7:
        actions.append("If symptoms persist/worsen, contact the provider for guidance.")
    if max_stress is not None and max_stress >= 8:
        actions.append("Reduce burnout: delegate one task + schedule a daily recovery break.")
    if upcoming_text:
        actions.append("Prepare 3 questions + bring meds list and symptom timeline.")

    if not actions:
        actions.append("Continue daily check-ins to strengthen pattern detection.")

    return summary_lines, actions, flags

# -------------------------
# Charts
# -------------------------
def render_charts(recipient_id: int):
    st.markdown("## 📈 Trends & Charts")

    # Check-in trends (30 days)
    cdf = query_df("""
        SELECT checkin_date, symptom_severity, caregiver_stress
        FROM checkins
        WHERE recipient_id = ?
          AND date(checkin_date) >= date('now','-30 day')
        ORDER BY date(checkin_date) ASC
    """, [recipient_id])

    if cdf.empty:
        st.info("No check-in data available for charts yet.")
    else:
        cdf["checkin_date"] = pd.to_datetime(cdf["checkin_date"])

        # Symptom severity chart
        fig1 = plt.figure()
        plt.plot(cdf["checkin_date"], cdf["symptom_severity"])
        plt.title("Symptom Severity (Last 30 Days)")
        plt.xlabel("Date")
        plt.ylabel("Severity (0–10)")
        plt.xticks(rotation=45)
        st.pyplot(fig1)

        # Caregiver stress chart
        fig2 = plt.figure()
        plt.plot(cdf["checkin_date"], cdf["caregiver_stress"])
        plt.title("Caregiver Stress (Last 30 Days)")
        plt.xlabel("Date")
        plt.ylabel("Stress (0–10)")
        plt.xticks(rotation=45)
        st.pyplot(fig2)

    # Med adherence (14 days)
    mdf = query_df("""
        SELECT ml.log_date, ml.status
        FROM med_log ml
        JOIN medications m ON m.id = ml.medication_id
        WHERE m.recipient_id = ?
          AND date(ml.log_date) >= date('now','-14 day')
    """, [recipient_id])

    if mdf.empty:
        st.info("No medication logs available for adherence chart yet.")
    else:
        mdf["log_date"] = pd.to_datetime(mdf["log_date"])
        daily = mdf.groupby(["log_date", "status"]).size().unstack(fill_value=0).reset_index()
        if "taken" not in daily.columns:
            daily["taken"] = 0
        if "missed" not in daily.columns:
            daily["missed"] = 0

        fig3 = plt.figure()
        plt.plot(daily["log_date"], daily["taken"], label="Taken")
        plt.plot(daily["log_date"], daily["missed"], label="Missed")
        plt.title("Medication Logs (Last 14 Days)")
        plt.xlabel("Date")
        plt.ylabel("Count")
        plt.xticks(rotation=45)
        plt.legend()
        st.pyplot(fig3)

# -------------------------
# PDF Export
# -------------------------
def generate_doctor_summary_pdf(filename: str, recipient_name: str, summary_lines, actions, flags):
    c = canvas.Canvas(filename, pagesize=letter)
    width, height = letter

    y = height - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, "CarePilot AI — Doctor Visit Summary")
    y -= 30

    c.setFont("Helvetica", 12)
    c.drawString(50, y, f"Care Recipient: {recipient_name}")
    y -= 20
    c.drawString(50, y, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    y -= 30

    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Summary")
    y -= 18
    c.setFont("Helvetica", 11)
    for line in summary_lines:
        txt = line.replace("**", "")
        c.drawString(60, y, f"- {txt}")
        y -= 14
        if y < 80:
            c.showPage()
            y = height - 60

    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Recommended Next Actions")
    y -= 18
    c.setFont("Helvetica", 11)
    for a in actions:
        c.drawString(60, y, f"- {a}")
        y -= 14
        if y < 80:
            c.showPage()
            y = height - 60

    y -= 10
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Detected Risk Flags")
    y -= 18
    c.setFont("Helvetica", 11)
    if not flags:
        c.drawString(60, y, "- None")
        y -= 14
    else:
        for f in flags:
            c.drawString(60, y, f"- {f['level']}: {f['title']} — {f['detail']}")
            y -= 14
            if y < 80:
                c.showPage()
                y = height - 60

    c.save()

# -------------------------
# UI helpers
# -------------------------
def select_recipient():
    recipients = query_df("""
        SELECT id, first_name, last_name, relationship, age
        FROM care_recipient
        ORDER BY id DESC
    """)
    if recipients.empty:
        st.info("No care recipient yet. Add one in **Profile**.")
        return None, None

    options = {
        f"{row['first_name']} {row['last_name']} ({row.get('relationship','')}, {row.get('age','?')}y)": int(row["id"])
        for _, row in recipients.iterrows()
    }
    choice = st.selectbox("Select care recipient", list(options.keys()))
    return options[choice], choice.split(" (")[0]

# -------------------------
# Pages
# -------------------------
def page_profile():
    st.subheader("Profile (Care Recipient)")

    with st.form("add_recipient"):
        c1, c2 = st.columns(2)
        with c1:
            first_name = st.text_input("First name*", placeholder="e.g., Mary")
        with c2:
            last_name = st.text_input("Last name*", placeholder="e.g., Johnson")

        relationship = st.text_input("Your relationship", placeholder="e.g., Mother, Uncle")
        age = st.number_input("Age", min_value=0, max_value=120, value=70)
        conditions = st.text_area("Known conditions (comma-separated)", placeholder="e.g., diabetes, hypertension")
        submitted = st.form_submit_button("Save care recipient")

    if submitted:
        if not first_name.strip() or not last_name.strip():
            st.error("First and last name are required.")
        else:
            execute(
                """INSERT INTO care_recipient
                   (first_name, last_name, relationship, age, conditions, created_at)
                   VALUES (?,?,?,?,?,?)""",
                [
                    first_name.strip(),
                    last_name.strip(),
                    relationship.strip(),
                    int(age),
                    conditions.strip(),
                    datetime.utcnow().isoformat()
                ]
            )
            st.success("Saved! Go to Dashboard and select the care recipient.")

    st.divider()
    st.markdown("### Existing care recipients")
    st.dataframe(
        query_df("SELECT id, first_name, last_name, relationship, age, conditions, created_at FROM care_recipient ORDER BY id DESC"),
        use_container_width=True
    )

def page_medications(recipient_id):
    st.subheader("Medications")

    with st.form("add_med"):
        med_name = st.text_input("Medication name*", placeholder="e.g., Metformin")
        dose = st.text_input("Dose", placeholder="e.g., 500mg")
        schedule = st.text_input("Schedule", placeholder="e.g., Morning, Evening")
        notes = st.text_area("Notes", placeholder="Any special instructions")
        add = st.form_submit_button("Add medication")

    if add:
        if not med_name.strip():
            st.error("Medication name is required.")
        else:
            execute("""
                INSERT INTO medications (recipient_id, med_name, dose, schedule, notes, active, created_at)
                VALUES (?,?,?,?,?,1,?)
            """, [recipient_id, med_name.strip(), dose.strip(), schedule.strip(), notes.strip(), datetime.utcnow().isoformat()])
            st.success("Medication added.")

    st.divider()

    meds = query_df("""
        SELECT id, med_name, dose, schedule, notes, active
        FROM medications
        WHERE recipient_id = ?
        ORDER BY id DESC
    """, [recipient_id])

    if meds.empty:
        st.info("No medications yet.")
        return

    st.markdown("### Current medications")
    st.dataframe(meds.drop(columns=["id"]), use_container_width=True)

    st.markdown("### Log medication status (today)")
    today_str = date.today().isoformat()

    for _, row in meds.iterrows():
        if int(row["active"]) != 1:
            continue
        med_id = int(row["id"])
        label = f"{row['med_name']} ({row.get('dose','')}) — {row.get('schedule','')}"
        c1, c2, c3 = st.columns([3, 1, 1])
        c1.write(label)

        if c2.button("Taken", key=f"taken_{med_id}"):
            execute("""
                INSERT INTO med_log (medication_id, log_date, status, logged_at)
                VALUES (?,?,?,?)
            """, [med_id, today_str, "taken", datetime.utcnow().isoformat()])
            st.success(f"Logged taken: {row['med_name']}")

        if c3.button("Missed", key=f"missed_{med_id}"):
            execute("""
                INSERT INTO med_log (medication_id, log_date, status, logged_at)
                VALUES (?,?,?,?)
            """, [med_id, today_str, "missed", datetime.utcnow().isoformat()])
            st.warning(f"Logged missed: {row['med_name']}")

    st.divider()
    st.markdown("### Medication log (last 14 days)")
    logs = query_df("""
        SELECT ml.log_date, m.med_name, ml.status, ml.logged_at
        FROM med_log ml
        JOIN medications m ON m.id = ml.medication_id
        WHERE m.recipient_id = ?
          AND date(ml.log_date) >= date('now','-14 day')
        ORDER BY date(ml.log_date) DESC, ml.logged_at DESC
    """, [recipient_id])

    if logs.empty:
        st.write("No logs yet.")
    else:
        st.dataframe(logs, use_container_width=True)

def page_appointments(recipient_id):
    st.subheader("Appointments")

    with st.form("add_appt"):
        appt_date = st.date_input("Date", value=date.today())
        appt_time = st.time_input("Time", value=dtime(10, 0))
        provider = st.text_input("Provider", placeholder="e.g., Dr. Smith")
        purpose = st.text_input("Purpose", placeholder="e.g., Follow-up")
        location = st.text_input("Location", placeholder="e.g., Clinic name")
        notes = st.text_area("Notes")
        add = st.form_submit_button("Add appointment")

    if add:
        dt = datetime.combine(appt_date, appt_time)
        execute("""
            INSERT INTO appointments (recipient_id, appt_datetime, provider, purpose, location, notes, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, [recipient_id, dt.isoformat(), provider.strip(), purpose.strip(), location.strip(), notes.strip(), datetime.utcnow().isoformat()])
        st.success("Appointment added.")

    st.divider()
    st.markdown("### Appointments (recent + upcoming)")
    appts = query_df("""
        SELECT appt_datetime, provider, purpose, location, notes
        FROM appointments
        WHERE recipient_id = ?
        ORDER BY datetime(appt_datetime) DESC
        LIMIT 50
    """, [recipient_id])

    if appts.empty:
        st.write("No appointments yet.")
    else:
        st.dataframe(appts, use_container_width=True)

def page_checkins(recipient_id):
    st.subheader("Daily Check-in")

    with st.form("add_checkin"):
        cdate = st.date_input("Date", value=date.today())
        symptoms = st.text_area("Symptoms (what did you notice?)", placeholder="e.g., fatigue, low appetite, dizziness")
        severity = st.slider("Symptom severity (0–10)", 0, 10, 3)
        caregiver_stress = st.slider("Your stress level today (0–10)", 0, 10, 4)
        free_text = st.text_area("Other notes", placeholder="Anything else important today?")
        add = st.form_submit_button("Save check-in")

    if add:
        execute("""
            INSERT INTO checkins (recipient_id, checkin_date, symptoms, symptom_severity, caregiver_stress, free_text, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, [recipient_id, cdate.isoformat(), symptoms.strip(), int(severity), int(caregiver_stress), free_text.strip(), datetime.utcnow().isoformat()])
        st.success("Check-in saved.")

    st.divider()
    st.markdown("### Recent check-ins")
    df = query_df("""
        SELECT checkin_date, symptom_severity, caregiver_stress, symptoms, free_text
        FROM checkins
        WHERE recipient_id = ?
        ORDER BY date(checkin_date) DESC
        LIMIT 30
    """, [recipient_id])

    if df.empty:
        st.write("No check-ins yet.")
    else:
        st.dataframe(df, use_container_width=True)

def page_dashboard(recipient_id, recipient_name):
    st.subheader("Dashboard")

    # AI Summary block
    st.markdown("## 🧠 CarePilot AI Summary")
    summary_lines, actions, flags = build_ai_summary(recipient_id)

    with st.container(border=True):
        for line in summary_lines:
            st.markdown(line)

        st.markdown("**Recommended next actions:**")
        for a in actions:
            st.write(f"• {a}")

        with st.expander("Show detected risk flags"):
            if not flags:
                st.write("No risk flags detected.")
            else:
                for f in flags:
                    st.write(f"- **{f['level']}** — {f['title']}: {f['detail']}")

    st.divider()

    # Charts
    render_charts(recipient_id)

    st.divider()

    # PDF Export
    st.markdown("## 🧾 Export for Doctor Visit")
    pdf_name = f"doctor_summary_{recipient_id}.pdf"
    if st.button("Generate PDF Summary"):
        generate_doctor_summary_pdf(pdf_name, recipient_name, summary_lines, actions, flags)
        st.success("PDF generated. Use the download button below.")

    if os.path.exists(pdf_name):
        with open(pdf_name, "rb") as f:
            st.download_button(
                label="Download Doctor Summary (PDF)",
                data=f,
                file_name=pdf_name,
                mime="application/pdf"
            )

# -------------------------
# Main app
# -------------------------
def main():
    st.set_page_config(page_title="CarePilot AI (MVP)", layout="wide")
    init_db()

    st.title("CarePilot AI (MVP)")
    st.caption("Care coordination + risk flags + AI Summary + charts + PDF export. Built with Streamlit + SQLite.")

    st.sidebar.header("Navigation")
    page = st.sidebar.radio("Go to", ["Dashboard", "Profile", "Medications", "Appointments", "Daily Check-in"])

    st.sidebar.divider()
    if st.sidebar.button("⚠️ Reset database (deletes all data)"):
        reset_db()
        st.sidebar.success("Database deleted. Restart the app.")
        st.stop()

    recipient_id = None
    recipient_name = None
    if page != "Profile":
        recipient_id, recipient_name = select_recipient()
        if recipient_id is None:
            st.stop()

    if page == "Dashboard":
        page_dashboard(recipient_id, recipient_name)
    elif page == "Profile":
        page_profile()
    elif page == "Medications":
        page_medications(recipient_id)
    elif page == "Appointments":
        page_appointments(recipient_id)
    elif page == "Daily Check-in":
        page_checkins(recipient_id)

if __name__ == "__main__":
    main()

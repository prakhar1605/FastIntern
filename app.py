# app.py
import streamlit as st
import pandas as pd
import re
import base64
from urllib.parse import urlparse
from dateutil import parser as dateparser
import time
import datetime
import json

# Try to import googleapiclient; we'll gracefully fall back to direct requests if unavailable
try:
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    HAS_GOOGLE_CLIENT = True
except Exception:
    HAS_GOOGLE_CLIENT = False

import requests

# ========== CONFIG ==========
# Replace these with your actual keys and IDs if needed.
GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
GOOGLE_CSE_ID = st.secrets["GOOGLE_CSE_ID"]


PORTAL_SITES = [
    "internshala.com",
    "naukri.com",
    "linkedin.com/jobs",
    "unstop.com",
    "careers.microsoft.com",
    "careers.google.com"
]

ROLE_SYNONYMS = {
    "data science": ["data science", "data scientist", "machine learning", "ml", "ai", "analytics", "data analyst"],
    "web development": ["web dev", "web developer", "frontend", "backend", "full stack", "javascript", "react", "node", "web development"],
    "software engineer": ["software engineer", "software developer", "backend", "frontend", "full stack", "sde"],
}

# Mapping for dateRestrict + sort
TIME_FILTERS = {
    "Anytime (Date Sorted)": (None, "date"),
    "Last 24 Hours": ("d1", "date"),
    "Last Week": ("d7", "date"),
    "Last Month": ("d30", "date"),
}

# ========== HELPERS ==========
def build_query(role: str, city: str, compensation: str):
    """
    Build a strict query that prefers exact-phrase and title matches, and restricts to common portals.
    Ensures 'intern' is present in the role phrase.
    """
    portals = " OR ".join([f"site:{p}" for p in PORTAL_SITES])
    comp_term = compensation if compensation and compensation != "either" else ""
    city_term = city if city and city != "Any" else ""

    role_clean = role.strip()
    if "intern" not in role_clean.lower():
        role_clean = role_clean + " intern"

    quoted = f'"{role_clean}"'
    intitle = f'intitle:"{role_clean}"'

    query = f'({quoted} OR {intitle}) {comp_term} {city_term} (apply OR internship OR "job-listing") ({portals})'
    return " ".join(query.split())


def detect_source(link: str):
    if not link:
        return "Unknown"
    net = urlparse(link).netloc.lower()
    if "internshala" in net: return "Internshala"
    if "naukri" in net: return "Naukri"
    if "linkedin" in net: return "LinkedIn"
    if "unstop" in net: return "Unstop"
    if "microsoft" in net: return "Microsoft Careers"
    if "google" in net: return "Google Careers"
    return net.replace("www.", "")


def prettify_title(title):
    if not title or not isinstance(title, str):
        return "No title"
    title = re.sub(r'\s*\((internshala|naukri|unstop|linkedin|indeed|glassdoor)[^\)]*\)\s*$', '', title, flags=re.I)
    parts = re.split(r'\s-\s', title)
    if len(parts) > 1:
        last = parts[-1]
        if re.search(r'(internshala|naukri|unstop|linkedin|internships|internship|bengaluru|bangalore|chennai|delhi|mumbai|page \d+)$', last, flags=re.I):
            title = parts[0]
        else:
            title = " - ".join(parts)
    title = title.strip()
    if len(title) > 80:
        part = title[:77]
        if ' ' in part:
            title = part.rsplit(' ',1)[0] + "..."
        else:
            title = part + "..."
    return title


def extract_published_date(item):
    """
    Try multiple places to find a publish date.
    Returns a pandas.Timestamp or None
    """
    pagemap = item.get("pagemap", {}) or {}
    metatags = pagemap.get("metatags", []) if isinstance(pagemap, dict) else []
    date_str = None

    # 1) try pagemap metatags
    for meta in metatags:
        if not isinstance(meta, dict):
            continue
        for k in ("article:published_time", "og:updated_time", "datePublished", "pubdate", "publication_date", "publishdate"):
            if k in meta and meta[k]:
                date_str = meta[k]
                break
        if date_str:
            break

    # 2) try item['snippet'] and title for patterns
    if not date_str:
        snippet = item.get("snippet", "") or ""
        title = item.get("title", "") or ""
        text_to_search = f"{title} {snippet}"

        iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text_to_search)
        if iso_match:
            date_str = iso_match.group(1)
        else:
            # dd MMM YYYY or d MMM YYYY
            m = re.search(r"\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{4})\b", text_to_search, flags=re.I)
            if m:
                date_str = m.group(1)
            else:
                m2 = re.search(r"posted\s+on\s+([^\.\n,]{6,30})", text_to_search, flags=re.I)
                if m2:
                    date_str = m2.group(1)

    if date_str:
        try:
            dt = dateparser.parse(date_str, fuzzy=True)
            if dt:
                return pd.to_datetime(dt)
        except Exception:
            return None
    return None


def csv_download_link(df, fname="fastintern_cards.csv"):
    df_cleaned = df.drop(columns=["Raw Title", "Snippet", "Combined Text"], errors='ignore')
    csv = df_cleaned.to_csv(index=False)
    b64 = base64.b64encode(csv.encode()).decode()
    return f'<a href="data:file/csv;base64,{b64}" download="{fname}" style="text-decoration:none; padding:10px 16px; background:#FF4B4B; color:white; border-radius:8px;">⬇️ Download CSV</a>'


# ========== SEARCH BACKENDS ==========
def google_cse_via_client(query: str, start: int = 1, num: int = 10, sort_param: str = None, date_restrict: str = None):
    """
    Use googleapiclient to call CSE. Returns (items_list, raw_response_dict)
    """
    if not HAS_GOOGLE_CLIENT:
        raise RuntimeError("googleapiclient not available in this environment.")
    service = build("customsearch", "v1", developerKey=GOOGLE_API_KEY)
    params = {"q": query, "cx": GOOGLE_CSE_ID, "start": start, "num": num}
    if sort_param:
        params["sort"] = sort_param
    if date_restrict:
        params["dateRestrict"] = date_restrict
    resp = service.cse().list(**params).execute()
    items = resp.get("items", [])
    return items, resp


def google_cse_via_rest(query: str, start: int = 1, num: int = 10, date_restrict: str = None, sort_param: str = None):
    """
    Direct REST fallback to Google's Custom Search JSON API using requests.
    Returns (items_list, raw_response_dict, status_code)
    """
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CSE_ID, "q": query, "start": start, "num": num}
    # dateRestrict is supported by the API as a URL parameter
    if date_restrict:
        params["dateRestrict"] = date_restrict
    # sort can be passed as well
    if sort_param:
        params["sort"] = sort_param
    r = requests.get(url, params=params, timeout=15)
    try:
        j = r.json()
    except Exception:
        j = {"error": {"message": "Failed to parse JSON response"}, "raw_text": r.text[:2000]}
    items = j.get("items", [])
    return items, j, r.status_code


def google_search_cse(query: str, max_results: int = 50, sort_param: str = None, date_restrict: str = None, pause_between_calls: float = 0.5, debug: bool = False):
    """
    Robust wrapper: try googleapiclient first (if available), otherwise fallback to REST.
    If googleapiclient returns an HttpError, try REST fallback.
    Returns collected_items, debug_info
    where debug_info is a dict with captured responses/errors for UI display if debug=True
    """
    collected = []
    debug_info = {"attempts": []}
    start = 1

    while len(collected) < max_results:
        try:
            num = min(10, max_results - len(collected))
            # try client library first if available
            if HAS_GOOGLE_CLIENT:
                try:
                    items, resp = google_cse_via_client(query, start=start, num=num, sort_param=sort_param, date_restrict=date_restrict)
                    debug_info["attempts"].append({"method": "client", "start": start, "num": num, "resp_top_keys": list(resp.keys()) if isinstance(resp, dict) else None})
                except HttpError as he:
                    # record the HttpError and fall back to REST
                    content = None
                    try:
                        content = he.content.decode() if isinstance(he.content, (bytes, bytearray)) else he.content
                    except Exception:
                        content = str(he)
                    debug_info["attempts"].append({"method": "client_error", "start": start, "num": num, "error": content})
                    # fall through to REST fallback
                    items, resp_rest, status = google_cse_via_rest(query, start=start, num=num, date_restrict=date_restrict, sort_param=sort_param)
                    debug_info["attempts"].append({"method": "rest_fallback", "start": start, "num": num, "status": status, "resp": resp_rest})
            else:
                # use REST directly
                items, resp_rest, status = google_cse_via_rest(query, start=start, num=num, date_restrict=date_restrict, sort_param=sort_param)
                debug_info["attempts"].append({"method": "rest", "start": start, "num": num, "status": status, "resp": resp_rest})

            if not items:
                # stop paging if this page had no items
                break
            collected.extend(items)
            start += num
            time.sleep(pause_between_calls)
        except Exception as e:
            debug_info.setdefault("exceptions", []).append(str(e))
            break

    return collected, debug_info


# ========== PARSE, CLASSIFY & EXTRACT DATES ==========
def parse_and_prepare(items, default_city):
    jobs = []
    for it in items:
        raw_title = it.get("title", "No title")
        title = prettify_title(raw_title)
        snippet = it.get("snippet", "") or ""
        link = it.get("link", "") or ""
        combined_text = f"{title}. {snippet}".strip()
        src = detect_source(link)

        # Location detection (basic)
        city = default_city
        for c in ["Bangalore", "Bengaluru", "Hyderabad", "Delhi", "NCR", "Mumbai", "Pune", "Chennai", "Remote"]:
            if c.lower() in combined_text.lower():
                city = c
                break

        # Published date best-effort
        pub_date = extract_published_date(it)

        job = {
            "Title": title,
            "Raw Title": raw_title,
            "Snippet": snippet,
            "Source": src,
            "Application Link": link,
            "Location": city,
            "Published Date": pub_date,
            "Combined Text": combined_text
        }
        jobs.append(job)
    return jobs


# ========== ROLE-STRICT ==========
def build_role_keywords(role_text: str):
    rt = role_text.lower().strip()
    tokens = [t for t in re.findall(r"[a-zA-Z0-9]+", rt) if t not in ("intern", "internship", "job", "jobs", "the", "for", "and", "or")]
    return tokens


def matches_role_strict(combined_text_lower: str, role_keywords: list, role_phrase: str = None):
    # Exact phrase match (best)
    if role_phrase and role_phrase.lower() in combined_text_lower:
        return True
    # postings must mention 'intern'/'internship'
    if "intern" not in combined_text_lower and "internship" not in combined_text_lower:
        return False
    # require all core keywords (if any) to appear
    if role_keywords:
        return all(kw in combined_text_lower for kw in role_keywords)
    return True


# ========== STREAMLIT UI ==========
st.set_page_config(page_title="FastIntern — Latest Jobs", layout="wide", initial_sidebar_state="expanded")
st.markdown("### 🚀 FastIntern — Latest Job Listings (Role-Strict)")
st.markdown("---")

with st.sidebar:
    st.header("Search Filters")
    role = st.text_input("Role (e.g., Data Science Intern)", value="Data Science Intern")
    city = st.selectbox("Preferred Location", ["Any", "Bangalore", "Hyderabad", "Delhi", "Mumbai", "Pune", "Chennai", "Remote"], index=1)
    compensation = st.selectbox("Compensation preference", ["either", "paid", "unpaid"], index=0)
    time_filter_key = st.selectbox("Job Posted Time", list(TIME_FILTERS.keys()), index=0, help="Filters results to a specific posting date range using Google's dateRestrict.")
    max_results = st.slider("Max results to fetch", 10, 100, 50, step=10, help="We page Google CSE automatically (10 per request).")
    debug_mode = st.checkbox("Debug mode (show API responses/errors)", value=False)
    relax_date_enforcement = st.checkbox("Relax date enforcement (show CSE results even if we can't parse a publish date)", value=False)
    run_search = st.button("🔎 Search internships", use_container_width=True)

if run_search:
    st.info(f"Searching for **{role}** posted in **{time_filter_key}**...")
    q = build_query(role, city if city != "Any" else "", compensation)
    date_restrict, sort_by = TIME_FILTERS[time_filter_key]

    items, debug_info = google_search_cse(q, max_results=max_results, sort_param=sort_by, date_restrict=date_restrict, debug=debug_mode)

    if debug_mode:
        st.subheader("Debug: query & API responses")
        st.write("CSE Query:", q)
        st.write("CSE Params:", {"dateRestrict": date_restrict, "sort": sort_by, "max_results": max_results})
        st.write("Debug info (attempts):")
        st.json(debug_info)

    if not items:
        st.warning("No results from Google CSE. Try a different query, remove the time filter, enable 'Relax date enforcement', or check your API / CSE credentials.")
    else:
        jobs = parse_and_prepare(items, default_city=(city if city != "Any" else "Any"))
        df = pd.DataFrame(jobs)

        # Role-strict filtering
        role_phrase = role.strip().lower()
        role_keys = build_role_keywords(role)
        df["role_match"] = df["Combined Text"].apply(lambda t: matches_role_strict(t.lower(), role_keys, role_phrase))
        df = df[df["role_match"] == True].drop(columns=["role_match"])

        if df.empty:
            st.warning("No job postings matched your role keywords. Try a broader role or Location='Any'.")
        else:
            # Normalize Published Date, and if time filter is used, optionally enforce threshold
            df["Published Date"] = pd.to_datetime(df["Published Date"], errors="coerce")

            if date_restrict and not relax_date_enforcement:
                m = re.match(r'd(\d+)', date_restrict)
                days = int(m.group(1)) if m else None
                if days is not None:
                    now = pd.Timestamp.utcnow()
                    threshold = now - pd.Timedelta(days=days)
                    before_count = len(df)
                    df = df[df["Published Date"].notna() & (df["Published Date"] >= threshold)]
                    after_count = len(df)
                    if df.empty:
                        st.warning(
                            "No postings with a verifiable publish date were found inside that time window. "
                            "Google date filtering may include pages without an explicit, parseable date. "
                            "Try enabling 'Relax date enforcement' to see CSE results even if we cannot parse dates."
                        )
                        st.markdown(f"_Found {before_count} role-matching cards from CSE, but only {after_count} had parseable dates within the last {days} day(s)._")

            df_sorted = df.sort_values(by="Published Date", ascending=False, na_position="last").reset_index(drop=True)

            if df_sorted.empty:
                st.warning("No job postings remain after applying strict filters (role + date). Try relaxing filters.")
            else:
                st.markdown(f"**✅ Found {len(df_sorted)} role-matching job cards.**")
                st.markdown("---")
                for idx, row in df_sorted.iterrows():
                    title = row["Title"]
                    src = row["Source"]
                    loc = row["Location"]
                    snippet = row["Snippet"]
                    link = row["Application Link"]
                    pub = row["Published Date"]
                    pub_str = pub.strftime("%B %d, %Y") if pd.notna(pub) else "Date: —"
                    short = (snippet[:120].rstrip() + "...") if isinstance(snippet, str) and len(snippet) > 120 else (snippet if snippet else "—")
                    st.markdown(f"""
                    ### 💼 {title}
                    - **Source:** `{src}` | **Location:** `{loc}` | **Posted:** `{pub_str}`
                    - **Snippet:** {short}
                    - **[Apply Now ➡️]({link})**
                    ---
                    """)
                st.markdown(csv_download_link(df_sorted, "fastintern_latest_jobs.csv"), unsafe_allow_html=True)
else:
    st.markdown("Enter your desired **Role** (e.g., Data Science Intern) and **Time Filter** in the sidebar, then click **Search**. Use **Debug mode** if you want to inspect raw API responses/errors.")

# Footer guidance when no results frequently appear
st.markdown("---")
st.markdown(
    "**Troubleshooting tips:**\n\n"
    "- If you repeatedly get no results, verify your **GOOGLE_API_KEY** and **GOOGLE_CSE_ID** are correct and that the API key has the Custom Search JSON API enabled and is not blocked by key restrictions.\n"
    "- If `Last 24 Hours` returns nothing but `Anytime` returns items, your CSE is returning results that lack parseable publish dates—enable 'Relax date enforcement' to view them.\n"
    "- Use `Debug mode` to see the raw JSON responses and exact error messages from Google (useful for identifying `KEY_RESTRICTED`, `dailyLimitExceeded`, or `cx` errors)."
)

import os
import sqlite3
import traceback

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)

TED_API_URL = "https://api.ted.europa.eu/v3/notices/search"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Common Procurement Vocabulary codes covering construction works (division 45):
# site preparation, building construction, building installation, and building
# completion/finishing works.
DEFAULT_CPV_CODES = ["45100000", "45200000", "45300000", "45400000"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_get(url: str) -> requests.Response:
    """Perform a GET request with standard timeout/redirect settings."""
    r = requests.get(url, timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r


def _build_cpv_or_clause(codes: list[str]) -> str:
    """Build a TED CPV OR-clause from a list of CPV codes."""
    return "(" + " OR ".join([f'classification-cpv = "{c}"' for c in codes]) + ")"


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------


def fetch_html_as_text(url: str) -> str | None:
    try:
        r = _http_get(url)

        content_type = (r.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            print(f"[HTML] Not an HTML page: {content_type}")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        print(f"[HTML] Extracted {len(text)} characters from {url}")
        return text

    except Exception as e:
        print(f"[HTML] Failed to extract text from {url}: {e}")
        return None


def cpv_filter_clause(cpv: str) -> str:
    """Return a TED API filter clause for the given CPV code.

    If *cpv* is an exact 8-digit code (e.g. ``"45100000"``), a single
    equality clause is returned.  For any other value (including the
    shorthand ``"45"`` / ``"45*"`` or an empty string) the function
    returns an OR-clause covering all :data:`DEFAULT_CPV_CODES`.
    """
    cpv = (cpv or "").strip()

    if cpv.isdigit() and len(cpv) == 8:
        return f'classification-cpv = "{cpv}"'

    return _build_cpv_or_clause(DEFAULT_CPV_CODES)


def download_pdf(url: str, pubno: str) -> str | None:
    if not url:
        return None

    os.makedirs("data/tenders", exist_ok=True)

    try:
        print(f"[TED] Attempting download: {url}")

        r = _http_get(url)

        content_type = (r.headers.get("Content-Type") or "").lower()
        print(f"[TED] Content-Type for {pubno}: {content_type}")

        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            print(f"[TED] Not a PDF for {pubno}, skipping")
            return None

        path = os.path.join("data/tenders", f"{pubno}.pdf")
        with open(path, "wb") as f:
            f.write(r.content)

        print(f"[TED] Saved PDF for {pubno}: {path}")
        return path

    except Exception as e:
        print(f"[TED] Download failed for {pubno}: {e}")
        return None


def ted_notice_url(pubno: str) -> str:
    return f"https://ted.europa.eu/en/notice/{pubno}"


def fetch_live_tenders(country="IRL", cpv="45*", days=30, limit=55):
    cpv_clause = cpv_filter_clause(cpv)

    expert_query = (
        f'buyer-country = "{country}" '
        f'AND {cpv_clause} '
        f'AND publication-date >= today(-{days})'
    )

    payload = {
        "query": expert_query,
        "limit": limit,
        "fields": ["OPP-010-notice"],
    }

    try:
        print(f"[TED] Query: {expert_query}")
        print(f"[TED] Payload: {payload}")

        r = requests.post(
            TED_API_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Accept-Language": "en",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        print("[TED] Status:", r.status_code)
        print("[TED] Response body:", r.text[:4000])
        r.raise_for_status()

        data = r.json()
        raw = data.get("results") or data.get("notices") or data.get("items") or []

        print("[TED DEBUG] Top-level keys:", list(data.keys()))
        print(f"[TED] Search returned {len(raw)} notices")

        tenders = []

        for notice in raw:
            pubno = notice.get("publication-number") or notice.get("OPP-010-notice", "")

            title = (
                notice.get("notice-title")
                or notice.get("title")
                or notice.get("object")
                or f"Tender {pubno}"
            )

            buyer = notice.get("buyer-name") or "Unknown buyer"

            deadline = (
                notice.get("deadline-date-lot")
                or notice.get("deadline-receipt-tender-date")
                or ""
            )

            value = (
                notice.get("estimated-value-cur")
                or notice.get("total-value-cur")
                or notice.get("result-value-lot")
                or ""
            )

            description = (
                notice.get("description-proc")
                or notice.get("description-lot")
                or ""
            )

            cpv_value = notice.get("classification-cpv")
            if isinstance(cpv_value, list):
                cpv_codes = [str(x) for x in cpv_value if x]
            elif cpv_value:
                cpv_codes = [str(cpv_value)]
            else:
                cpv_codes = []

            links = notice.get("links", {})
            view_url = None
            pdf_url = None

            if isinstance(links, dict):
                html_links = links.get("html", {})
                pdf_links = links.get("pdf", {})

                if isinstance(html_links, dict):
                    view_url = html_links.get("ENG") or next(iter(html_links.values()), None)

                if isinstance(pdf_links, dict):
                    pdf_url = pdf_links.get("ENG") or next(iter(pdf_links.values()), None)

            tenders.append({
                "publication_number": pubno,
                "publication_date": (notice.get("publication-date", "") or "")[:10],
                "title": title,
                "buyer": buyer,
                "cpv_codes": cpv_codes,
                "country": country,
                "deadline": deadline,
                "value": value,
                "currency": "EUR",
                "description": description,
                "document_links": [pdf_url] if pdf_url else [],
                "pdf_local": None,
                "view_url": view_url or ted_notice_url(pubno),
            })

        print(f"[TED] Loaded {len(tenders)} tenders")
        return tenders

    except Exception as e:
        print(f"[TED] Exception: {e}")
        traceback.print_exc()
        return []


# ---------------------------------------------------------------------------
# QA / AI helpers
# ---------------------------------------------------------------------------


def load_agents_md():
    agent_file = os.path.join(os.path.expanduser("~"), ".openclaw", "workspace", "AGENTS.md")
    try:
        with open(agent_file, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def call_construction_ai(prompt):
    # Placeholder – replace with actual AI call
    return f"AI response for: {prompt[:80]}"


def get_db():
    db = sqlite3.connect("data/reilor.db")
    return db


def qa_service_stub():
    class _QA:
        def ask(self, question, top_k=3):
            return {"sources": []}

    return _QA()


qa_service = qa_service_stub()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/api/tenders")
def api_tenders_alias():
    return api_tenders_live()


@app.route("/pricing")
def pricing():
    return render_template("gtp_v1/pricing.html")


@app.get("/tenders")
def tenders():
    return render_template("gtp_v1/tender_process.html")


@app.route("/api/ai/agent", methods=["POST"])
def ai_agent():
    data = request.get_json(silent=True) or {}

    agent = (data.get("agent") or "SiteProjectManager").strip()
    question = (data.get("question") or "").strip()
    extra_context = (data.get("context") or "").strip()

    if not question:
        return jsonify({"error": "No question"}), 400

    try:
        agents_md = load_agents_md()
    except Exception:
        agents_md = ""

    try:
        tfidf_result = qa_service.ask(question, top_k=3)
        kb_context = "\n".join([s.get("snippet", "") for s in tfidf_result.get("sources", [])]).strip()
        sources = tfidf_result.get("sources", [])
    except Exception as e:
        print("[WARN] qa_service failed:", e)
        kb_context = ""
        sources = []

    prompt = f"""
You are the specialist agent: {agent}

Follow this agent roster:
{agents_md}

Rules:
- Ireland construction context
- Metric units
- EUR pricing
- Mention BCAR / HSA / RAMS where relevant

Knowledge context:
{kb_context}

Tender context:
{extra_context}

Question:
{question}
""".strip()

    answer = call_construction_ai(prompt)

    return jsonify({
        "answer": answer,
        "agent": agent,
        "sources": sources,
    })


@app.route("/design-home")
def design_home():
    return render_template("design_home.html")


@app.post("/api/ai/home-design")
def ai_home_design():
    data = request.get_json() or {}
    prompt = data.get("prompt", "")

    design = f"""
\U0001f3e1 Proposed Home Concept
------------------------

User Request:
{prompt}

Layout Suggestion
-----------------
Ground Floor:
- Open plan kitchen / dining (30m\u00b2)
- Living room (22m\u00b2)
- Utility room
- Guest WC

First Floor:
- Master bedroom with ensuite
- Two bedrooms
- Family bathroom

Exterior
--------
- Modern Irish design
- White render walls
- Slate roof
- Large south facing windows
- Solar panels

Estimated Build Size
--------------------
~165 m\u00b2

Estimated Build Cost (Ireland)
------------------------------
\u20ac280,000 \u2013 \u20ac340,000
"""

    return jsonify({"design": design})


@app.route("/design-home-3d")
def design_home_3d():
    return render_template("design_home_3d.html")


@app.post("/api/ai/home-schema")
def ai_home_schema():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip().lower()

    bedrooms = 3
    bathrooms = 2
    style = "Modern"
    floors = 1

    if "4 bed" in prompt or "4-bedroom" in prompt or "4 bedroom" in prompt:
        bedrooms = 4
    elif "2 bed" in prompt or "2-bedroom" in prompt or "2 bedroom" in prompt:
        bedrooms = 2

    if "3 bath" in prompt or "3 bathroom" in prompt:
        bathrooms = 3
    elif "1 bath" in prompt or "1 bathroom" in prompt:
        bathrooms = 1

    if "two storey" in prompt or "2 storey" in prompt or "2-storey" in prompt or "upstairs" in prompt:
        floors = 2

    if "farmhouse" in prompt:
        style = "Farmhouse"
    elif "traditional" in prompt:
        style = "Traditional"
    elif "modern" in prompt:
        style = "Modern"

    schema = {
        "meta": {
            "prompt": prompt,
            "style": style,
            "floors": floors,
        },
        "house": {
            "width": 16,
            "depth": 12,
            "wall_height": 3,
            "wall_thickness": 0.2,
            "roof_height": 2.2,
        },
        "rooms": [
            {"name": "Living Room", "x": 0.5, "z": 0.5, "w": 5.5, "d": 5.0, "floor": 0},
            {"name": "Kitchen / Dining", "x": 6.2, "z": 0.5, "w": 8.8, "d": 5.0, "floor": 0},
            {"name": "Hall", "x": 0.5, "z": 5.8, "w": 3.0, "d": 2.5, "floor": 0},
            {"name": "Bathroom", "x": 3.8, "z": 5.8, "w": 2.2, "d": 2.5, "floor": 0},
            {"name": "Bedroom 1", "x": 6.3, "z": 5.8, "w": 4.2, "d": 5.2, "floor": 0},
            {"name": "Bedroom 2", "x": 10.8, "z": 5.8, "w": 4.0, "d": 5.2, "floor": 0},
        ],
    }

    if bedrooms >= 3:
        schema["rooms"].append({"name": "Bedroom 3", "x": 0.5, "z": 8.7, "w": 5.5, "d": 2.3, "floor": 0})

    if bedrooms >= 4:
        schema["rooms"].append({"name": "Bedroom 4", "x": 6.3, "z": 8.7, "w": 4.2, "d": 2.3, "floor": 0})

    if floors == 2:
        schema["rooms"] = [
            {"name": "Living Room", "x": 0.5, "z": 0.5, "w": 5.0, "d": 5.0, "floor": 0},
            {"name": "Kitchen / Dining", "x": 5.8, "z": 0.5, "w": 8.7, "d": 5.0, "floor": 0},
            {"name": "WC", "x": 0.5, "z": 5.8, "w": 2.0, "d": 1.8, "floor": 0},
            {"name": "Utility", "x": 2.8, "z": 5.8, "w": 2.4, "d": 1.8, "floor": 0},
            {"name": "Hall / Stairs", "x": 5.5, "z": 5.8, "w": 3.0, "d": 2.5, "floor": 0},
            {"name": "Office / Playroom", "x": 8.8, "z": 5.8, "w": 5.7, "d": 2.5, "floor": 0},
            {"name": "Master Bedroom", "x": 0.5, "z": 0.5, "w": 5.5, "d": 4.8, "floor": 1},
            {"name": "Ensuite", "x": 0.5, "z": 5.6, "w": 2.2, "d": 2.0, "floor": 1},
            {"name": "Bedroom 2", "x": 6.2, "z": 0.5, "w": 4.0, "d": 3.6, "floor": 1},
            {"name": "Bedroom 3", "x": 10.5, "z": 0.5, "w": 4.0, "d": 3.6, "floor": 1},
            {"name": "Bathroom", "x": 6.2, "z": 4.5, "w": 2.6, "d": 2.5, "floor": 1},
            {"name": "Landing", "x": 9.1, "z": 4.5, "w": 5.4, "d": 2.5, "floor": 1},
        ]

    return jsonify(schema)


@app.get("/tender-process")
def tender_process_redirect():
    return redirect(url_for("tenders"), code=302)


@app.get("/tender-ui")
def tender_ui():
    return render_template("gtp_v1/tender_ui_embed.html")


@app.route("/api/tenders/live")
def api_tenders_live():
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 30))
    country = request.args.get("country", "IRL")
    cpv = request.args.get("cpv", "45*")

    tender_list = fetch_live_tenders(country=country, cpv=cpv, days=days, limit=limit)
    print("[API] Returning live tenders:", len(tender_list))
    return jsonify({"count": len(tender_list), "items": tender_list})


@app.route("/manpower", methods=["GET", "POST"])
def manpower():
    if request.method == "POST":
        print("Saving operator...")
        return render_template("gtp_v1/manpower.html", message="✅ Operator saved successfully")

    return render_template("gtp_v1/manpower.html")


@app.route("/jobs")
def jobs():
    sample_jobs = [
        {"title": "Groundworker", "location": "Cork", "company": "GroundTechPro", "pay": "€20–€24/hr", "type": "Full-time"},
        {"title": "360 Machine Driver", "location": "Dublin", "company": "Civil Works Ltd", "pay": "€23–€27/hr", "type": "Contract"},
        {"title": "Pipe Layer", "location": "Limerick", "company": "Munster Utilities", "pay": "€21–€25/hr", "type": "Full-time"},
    ]
    return render_template("gtp_v1/jobs.html", jobs=sample_jobs)


@app.route("/machine-power")
def machine_power():
    return render_template(
        "gtp_v1/machine_power.html",
        user_email=session.get("user_email"),
        role=session.get("user_role"),
    )


@app.route("/plant-hire")
def plant_hire():
    return redirect(url_for("machines_browse"))


@app.route("/machines/")
def machines_slash():
    return redirect(url_for("machines_browse"))


@app.route("/machines")
def machines_browse():
    q = (request.args.get("q") or "").strip()
    mtype = (request.args.get("type") or "").strip()
    county = (request.args.get("county") or "").strip()
    avail = (request.args.get("avail") or "").strip()

    sql = "SELECT * FROM machines WHERE 1=1"
    params = []

    if q:
        sql += " AND (name LIKE ? OR make LIKE ? OR model LIKE ? OR notes LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]

    if mtype:
        sql += " AND type = ?"
        params.append(mtype)

    if county:
        sql += " AND county = ?"
        params.append(county)

    if avail:
        sql += " AND availability_status = ?"
        params.append(avail)

    sql += " ORDER BY created_at DESC LIMIT 200"

    db = get_db()
    db.row_factory = sqlite3.Row
    machines = db.execute(sql, params).fetchall()
    db.close()

    return render_template(
        "gtp_v1/machines/browse.html",
        machines=machines,
        user_email=session.get("user_email"),
        role=session.get("user_role"),
    )


@app.route("/machines/register", methods=["GET", "POST"])
def machines_register():
    if request.method == "POST":
        data = {
            "owner_email": session.get("user_email"),
            "name": (request.form.get("name") or "").strip(),
            "type": (request.form.get("type") or "").strip(),
            "make": (request.form.get("make") or "").strip(),
            "model": (request.form.get("model") or "").strip(),
            "year": request.form.get("year") or None,
            "tonnage": request.form.get("tonnage") or None,
            "operator_included": 1 if request.form.get("operator_included") else 0,
            "fuel_type": (request.form.get("fuel_type") or "").strip(),
            "stage_v": 1 if request.form.get("stage_v") else 0,
            "location": (request.form.get("location") or "").strip(),
            "county": (request.form.get("county") or "").strip(),
            "rate_hour": request.form.get("rate_hour") or None,
            "rate_day": request.form.get("rate_day") or None,
            "rate_week": request.form.get("rate_week") or None,
            "availability_status": (request.form.get("availability_status") or "AVAILABLE").strip(),
            "notes": (request.form.get("notes") or "").strip(),
        }

        if not data["name"]:
            return render_template(
                "gtp_v1/machines/register.html",
                message="Name is required.",
                user_email=session.get("user_email"),
                role=session.get("user_role"),
            )

        db = get_db()
        db.execute("""
            INSERT INTO machines
            (owner_email,name,type,make,model,year,tonnage,operator_included,fuel_type,stage_v,location,county,rate_hour,rate_day,rate_week,availability_status,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data["owner_email"], data["name"], data["type"], data["make"], data["model"], data["year"], data["tonnage"],
            data["operator_included"], data["fuel_type"], data["stage_v"], data["location"], data["county"],
            data["rate_hour"], data["rate_day"], data["rate_week"], data["availability_status"], data["notes"]
        ))
        db.commit()
        db.close()

        return redirect(url_for("machines_browse"))

    return render_template(
        "gtp_v1/machines/register.html",
        user_email=session.get("user_email"),
        role=session.get("user_role"),
    )


@app.route("/machines/evidence")
def machines_evidence():
    return redirect(url_for("machines_browse"))


@app.route("/machines/maintenance")
def machines_maintenance():
    return redirect(url_for("machines_browse"))


if __name__ == "__main__":
    app.run(debug=True)

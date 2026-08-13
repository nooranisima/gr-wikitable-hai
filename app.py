"""
Verifier Experiment — WTQ representations (Prolific data collection)
====================================================================
Verification-loop paper. Question: can a code-illiterate human verify an AI
analyst's step if it is shown in the right REPRESENTATION?

Design (within-subject, pairing-balanced):
  - Trial universe = every pool item x {code, strategy, goal_means}.
  - Each pairing should accumulate judgments evenly -> least-exposed-first
    assignment over PAIRINGS (committed + in-flight pending w/ TTL).
  - Per participant: TRIALS_PER_PARTICIPANT trials, mixed representations
    (at most REP_CAP of any one condition), and NEVER the same item twice
    in any representation.
  - Per trial: show question + full table + ONE representation of the model's
    SQL. Participant: approve / reject / can't tell + confidence 0-100 +
    free-text (mandatory on reject). Model's output/answer is WITHHELD.
  - No live model call: submit is instant. Feedback is fed to the model
    OFFLINE later (phase 2), which is why reject-feedback is mandatory.
  - Logged to Redis (primary, /admin/export) and Qualtrics (backup).

Env vars (Render dashboard):
  REDIS_URL, ADMIN_PASSWORD,
  QUALTRICS_DATACENTER, QUALTRICS_API_TOKEN, QUALTRICS_SURVEY_ID,
  PROLIFIC_COMPLETION_CODE (optional)
  Optional overrides: TRIALS_PER_PARTICIPANT (5), REP_CAP (3),
  MIN_FEEDBACK_CHARS (20), PENDING_TTL_S (3600)

Run: gunicorn app:app --threads 8 --timeout 60
"""

import json
import os
import random
import re
import time
import uuid

import redis
import requests
from flask import Flask, redirect, render_template_string, request, session, url_for

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
REPRESENTATIONS = ["code", "strategy", "goal_means"]
REP_LABEL = {
    "code": "the exact query code the analyst will run",
    "strategy": "a short summary of the analyst's approach",
    "goal_means": "the analyst's explanation of how the analysis answers the question",
}
TRIALS_PER_PARTICIPANT = int(os.environ.get("TRIALS_PER_PARTICIPANT", "5"))
REP_CAP = int(os.environ.get("REP_CAP", "3"))  # max trials of one representation per participant
MIN_FEEDBACK_CHARS = int(os.environ.get("MIN_FEEDBACK_CHARS", "20"))
PENDING_TTL_S = int(os.environ.get("PENDING_TTL_S", "3600"))

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
PROLIFIC_COMPLETION_CODE = os.environ.get("PROLIFIC_COMPLETION_CODE", "")

QUALTRICS_DATACENTER = os.environ.get("QUALTRICS_DATACENTER", "")
QUALTRICS_API_TOKEN = os.environ.get("QUALTRICS_API_TOKEN", "")
QUALTRICS_SURVEY_ID = os.environ.get("QUALTRICS_SURVEY_ID", "")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", uuid.uuid4().hex)

# ----------------------------------------------------------------------------
# Redis  (namespace vw: so it can coexist with other studies on one instance)
# ----------------------------------------------------------------------------
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
r = redis.from_url(REDIS_URL, decode_responses=True)

K_EXPOSURE = "vw:exposure:{pair}"        # int, SUBMITTED judgments for (item, rep)
K_PENDING = "vw:pending:{pair}"          # int w/ TTL, in-flight assignments
K_PARTICIPANT = "vw:participant:{pid}"   # json state
K_LOG = "vw:log:{pid}:{trial}"           # json full trial record
K_LOG_INDEX = "vw:log_index"             # list of log keys
K_TRIAL_COUNTER = "vw:global_trial_counter"

# ----------------------------------------------------------------------------
# Item bank: pool_data.json = wtq_pool_with_synthetic.json from the Colab run
# (see scripts/make_pool_data.py). Items missing any representation are skipped.
# ----------------------------------------------------------------------------
with open(os.path.join(os.path.dirname(__file__), "pool_data.json")) as f:
    _raw = json.load(f)
_items = _raw["items"] if isinstance(_raw, dict) else _raw
ITEMS = [it for it in _items
         if all((it.get("representations") or {}).get(rep) for rep in REPRESENTATIONS)]
ITEM_BY_ID = {it["id"]: it for it in ITEMS}
PAIRINGS = [(it["id"], rep) for it in ITEMS for rep in REPRESENTATIONS]
if isinstance(_raw, dict) and (_raw.get("meta") or {}).get("placeholder"):
    print("!! pool_data.json is the PLACEHOLDER — replace before launch")
print(f"Loaded {len(ITEMS)} items -> {len(PAIRINGS)} (item x representation) pairings")


def pair_key(item_id, rep):
    return f"{item_id}::{rep}"


# ----------------------------------------------------------------------------
# Qualtrics logging (backup sink; Redis /admin/export is authoritative)
# ----------------------------------------------------------------------------
def _qualtrics_base():
    dc = QUALTRICS_DATACENTER.strip().rstrip("/")
    if not dc:
        return ""
    if not dc.startswith("http"):
        dc = f"https://{dc}.qualtrics.com"
    return dc


def log_to_qualtrics(values):
    base = _qualtrics_base()
    if not (base and QUALTRICS_API_TOKEN and QUALTRICS_SURVEY_ID):
        print("[qualtrics] SKIPPED: credentials incomplete")
        return
    try:
        clean = {k: str(v) for k, v in values.items()}
        resp = requests.post(
            f"{base}/API/v3/surveys/{QUALTRICS_SURVEY_ID}/responses",
            headers={"X-API-TOKEN": QUALTRICS_API_TOKEN, "Content-Type": "application/json"},
            json={"values": clean},
            timeout=20,
        )
        if resp.status_code == 200:
            print(f"[qualtrics] OK trial={values.get('trial_overall')}")
        else:
            print(f"[qualtrics] FAILED {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        print(f"[qualtrics] EXCEPTION: {e}")


def log_trial(record):
    key = K_LOG.format(pid=record["participant_id"], trial=record["trial_overall"])
    r.set(key, json.dumps(record))
    r.rpush(K_LOG_INDEX, key)
    log_to_qualtrics(record)


# ----------------------------------------------------------------------------
# Assignment: least-exposed-first over (item, rep) pairings, with
#   (a) no repeated ITEM within a participant (any representation), and
#   (b) at most REP_CAP trials of one representation per participant.
# Exposure commits on SUBMIT; assignment places an expiring pending claim so
# abandoned sessions release their pairings after PENDING_TTL_S.
# ----------------------------------------------------------------------------
def assign_pairings(n):
    load = []
    for item_id, rep in PAIRINGS:
        pk = pair_key(item_id, rep)
        committed = int(r.get(K_EXPOSURE.format(pair=pk)) or 0)
        pending = int(r.get(K_PENDING.format(pair=pk)) or 0)
        load.append((committed + pending, random.random(), item_id, rep))
    load.sort()

    def greedy(rep_cap):
        chosen, items_used, rep_count = [], set(), {rep: 0 for rep in REPRESENTATIONS}
        for _, _, item_id, rep in load:
            if len(chosen) >= n:
                break
            if item_id in items_used or rep_count[rep] >= rep_cap:
                continue
            chosen.append((item_id, rep))
            items_used.add(item_id)
            rep_count[rep] += 1
        return chosen

    chosen = greedy(REP_CAP)
    if len(chosen) < n:                       # cap infeasible near end of collection
        chosen = greedy(n)                    # relax rep cap, keep item-dedup
    random.shuffle(chosen)
    for item_id, rep in chosen:
        pk = K_PENDING.format(pair=pair_key(item_id, rep))
        r.incr(pk)
        r.expire(pk, PENDING_TTL_S)
    return [[item_id, rep] for item_id, rep in chosen]


def get_state(pid):
    raw = r.get(K_PARTICIPANT.format(pid=pid))
    return json.loads(raw) if raw else None


def save_state(pid, state):
    r.set(K_PARTICIPANT.format(pid=pid), json.dumps(state))


# ----------------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------------
BASE_CSS = """
:root { --ink:#1c2733; --muted:#5b6b7b; --line:#d8dfe6; --bg:#f2f5f8;
        --card:#ffffff; --accent:#0b6e6e; --warn:#8a4b08; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI",system-ui,-apple-system,sans-serif;
       background:var(--bg); color:var(--ink); line-height:1.5; }
.wrap { max-width:980px; margin:0 auto; padding:24px 16px 64px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
        padding:24px; margin-bottom:18px; }
h1 { font-size:1.35rem; margin:0 0 8px; } h2 { font-size:1.05rem; margin:0 0 10px; }
.tag { display:inline-block; font-size:.78rem; letter-spacing:.06em; text-transform:uppercase;
       color:var(--accent); border:1px solid var(--accent); border-radius:999px;
       padding:2px 10px; margin-bottom:10px; }
.muted { color:var(--muted); font-size:.92rem; }
.qbox { background:#f7f9fb; border-left:4px solid var(--accent); padding:12px 14px;
        border-radius:0 8px 8px 0; white-space:pre-wrap; }
.aibox { background:#fdf6ec; border-left:4px solid var(--warn); padding:12px 14px;
         border-radius:0 8px 8px 0; white-space:pre-wrap; }
.aibox.code { font-family:ui-monospace,Consolas,monospace; font-size:.95rem; }
table.data { border-collapse:collapse; width:100%; font-size:.95rem; }
table.data th, table.data td { border:1px solid var(--line); padding:6px 10px; text-align:left; }
table.data th { background:#eef2f6; }
table.data tr:nth-child(even) td { background:#fafcfe; }
label { display:block; font-weight:600; margin:16px 0 6px; }
input[type=text], textarea { width:100%; padding:10px 12px; border:1px solid var(--line);
        border-radius:8px; font-size:1rem; font-family:inherit; }
textarea { min-height:110px; resize:vertical; }
.radio-row label { display:inline-block; font-weight:400; margin:0 18px 0 4px; }
input[type=range] { width:70%; vertical-align:middle; }
.confval { display:inline-block; min-width:3ch; font-weight:700; }
button { background:var(--accent); color:#fff; border:0; border-radius:8px;
         padding:12px 26px; font-size:1rem; font-weight:600; cursor:pointer; }
button:disabled { opacity:.5; cursor:wait; }
.progress { font-variant-numeric:tabular-nums; color:var(--muted); margin-bottom:12px; }
.err { color:#a01818; font-weight:600; }
ol li { margin-bottom:6px; }
.exbox { border:1px dashed var(--line); border-radius:8px; padding:12px 14px; }
"""

LANDING_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Check the AI analyst — data study</title><style>{{ css }}</style></head><body>
<div class="wrap">
  <div class="card">
    <span class="tag">Research study</span>
    <h1>Would this AI analysis answer the question correctly?</h1>
    <p>An AI data analyst was asked questions about small data tables. Before the
    analysis is run, <strong>you review it</strong>. On each of your
    <strong>{{ n_trials }} trials</strong> you will see:</p>
    <ol>
      <li>A question about a data table,</li>
      <li>the full table (usually about 10 rows), and</li>
      <li>the analyst's submission — sometimes the raw computer code it will run,
          sometimes a plain-language description of its approach.</li>
    </ol>
    <p><strong>Your job:</strong> decide whether the analyst's approach will produce
    the correct answer to the question <em>as asked</em>. Some submissions contain a
    subtle mistake — a wrong column, a wrong value, the wrong kind of calculation.
    Others are perfectly fine. You will NOT be shown the analysis result — judge the
    approach, not the answer.</p>
    <div class="exbox"><strong>Worked example.</strong> Question: <em>"in how many games
    did the winning team score more than 4 points?"</em> next to a table of soccer
    matches. If the submission says it <em>"adds both teams' goals together and keeps
    matches where the combined total exceeds 4"</em> — that is a mistake you can catch:
    a 1&ndash;4 match would be counted even though the winner scored only 4. You would
    mark it <em>wrong</em> and briefly say why.</p></div>
    <p class="muted">If a submission is computer code you cannot read, that's a valid
    situation — answer honestly, including "I can't tell". No programming knowledge is
    needed or expected. Please don't use external tools. About 8&ndash;12 minutes.</p>
    <form method="post" action="{{ url_for('start') }}">
      <label for="pid">Prolific ID</label>
      <input type="text" id="pid" name="pid" value="{{ pid or '' }}" required
             pattern="[A-Za-z0-9]{5,64}">
      <label>Can you read or write SQL (a database programming language)?</label>
      <div class="radio-row">
        <input type="radio" id="sqlk0" name="sql_knowledge" value="none" required>
        <label for="sqlk0">No</label>
        <input type="radio" id="sqlk1" name="sql_knowledge" value="some">
        <label for="sqlk1">A little</label>
        <input type="radio" id="sqlk2" name="sql_knowledge" value="fluent">
        <label for="sqlk2">Yes, comfortably</label>
      </div>
      <p class="muted">Answer honestly — it does not affect your payment or eligibility.</p>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      <p><button type="submit">Begin study</button></p>
    </form>
  </div>
</div></body></html>
"""

TRIAL_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trial {{ trial_num }} of {{ n_trials }}</title><style>{{ css }}</style></head><body>
<div class="wrap">
  <p class="progress">Trial {{ trial_num }} / {{ n_trials }}</p>

  <div class="card">
    <h2>The question the analyst was asked</h2>
    <div class="qbox">{{ question }}</div>
  </div>

  <div class="card">
    <h2>The data table</h2>
    <table class="data">
      <tr>{% for h in header %}<th>{{ h }}</th>{% endfor %}</tr>
      {% for row in rows %}<tr>{% for c in row %}<td>{{ c }}</td>{% endfor %}</tr>{% endfor %}
    </table>
  </div>

  <div class="card">
    <h2>The analyst's submission</h2>
    <p class="muted">You are shown: {{ rep_label }}.</p>
    <div class="aibox {{ 'code' if rep == 'code' else '' }}">{{ rep_text }}</div>
  </div>

  <div class="card">
    <h2>Your judgment</h2>
    <form method="post" action="{{ url_for('submit') }}" id="trialform">
      <label>1&nbsp;·&nbsp;Will this approach produce the correct answer to the question?</label>
      <div class="radio-row">
        <input type="radio" id="v_ok" name="verdict" value="approve" required>
        <label for="v_ok">Yes — approve it</label>
        <input type="radio" id="v_no" name="verdict" value="reject">
        <label for="v_no">No — something is wrong</label>
        <input type="radio" id="v_cant" name="verdict" value="cannot_verify">
        <label for="v_cant">I can't tell from what I'm shown</label>
      </div>

      <label for="confidence">2&nbsp;·&nbsp;How confident are you in that judgment?</label>
      <input type="range" id="confidence" name="confidence" min="0" max="100" value="50"
             oninput="document.getElementById('cv').textContent=this.value">
      <span class="confval" id="cv">50</span>/100

      <label for="feedback">3&nbsp;·&nbsp;Explain your judgment
        <span class="muted" id="fbreq">(required if you marked it wrong — say what is wrong
        and what the analyst should do instead, in your own words)</span></label>
      <textarea id="feedback" name="feedback"
        placeholder="e.g. 'It uses the attendance column, but the question asks about goals.'"></textarea>

      {% if error %}<p class="err">{{ error }}</p>{% endif %}
      <p><button type="submit" id="gobtn">Submit judgment</button></p>
    </form>
  </div>
</div>
<script>
const fb = document.getElementById('feedback');
document.querySelectorAll('input[name=verdict]').forEach(el =>
  el.addEventListener('change', () => {
    if (el.value === 'reject' && el.checked) {
      fb.required = true; fb.minLength = {{ min_chars }};
    } else if (el.checked) { fb.required = false; fb.removeAttribute('minlength'); }
  }));
document.getElementById('trialform').addEventListener('submit',
  () => { document.getElementById('gobtn').disabled = true; });
</script>
</body></html>
"""

DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Study complete</title><style>{{ css }}</style></head><body>
<div class="wrap"><div class="card">
  <span class="tag">Complete</span>
  <h1>Thank you — all {{ n_trials }} trials are done</h1>
  {% if code %}
  <p>Your Prolific completion code:</p>
  <div class="aibox"><div style="font-size:1.25rem;font-weight:700">{{ code }}</div></div>
  <p><a href="https://app.prolific.com/submissions/complete?cc={{ code }}">
     Return to Prolific and submit</a></p>
  {% else %}
  <p>You may now return to Prolific to complete your submission.</p>
  {% endif %}
</div></div></body></html>
"""

# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/")
def landing():
    pid = request.args.get("PROLIFIC_PID", "")
    return render_template_string(
        LANDING_HTML, css=BASE_CSS, pid=pid, n_trials=TRIALS_PER_PARTICIPANT, error=None
    )


@app.route("/start", methods=["POST"])
def start():
    pid = (request.form.get("pid") or "").strip()
    sql_knowledge = (request.form.get("sql_knowledge") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{5,64}", pid):
        return render_template_string(
            LANDING_HTML, css=BASE_CSS, pid=pid, n_trials=TRIALS_PER_PARTICIPANT,
            error="Please enter a valid Prolific ID.",
        )
    state = get_state(pid)
    if state is None:
        state = {
            "pairings": assign_pairings(TRIALS_PER_PARTICIPANT),
            "sql_knowledge": sql_knowledge,
            "trial_idx": 0,
            "started_at": time.time(),
            "completed": False,
        }
        save_state(pid, state)
    session["pid"] = pid
    return redirect(url_for("done") if state["completed"] else url_for("trial"))


def _render_trial(state, error=None):
    item_id, rep = state["pairings"][state["trial_idx"]]
    item = ITEM_BY_ID[item_id]
    session["trial_shown_at"] = time.time()
    return render_template_string(
        TRIAL_HTML,
        css=BASE_CSS,
        trial_num=state["trial_idx"] + 1,
        n_trials=len(state["pairings"]),
        question=item["question"],
        header=item["table_header"],
        rows=item["table_rows"],
        rep=rep,
        rep_label=REP_LABEL[rep],
        rep_text=item["representations"][rep],
        min_chars=MIN_FEEDBACK_CHARS,
        error=error,
    )


@app.route("/trial")
def trial():
    pid = session.get("pid")
    if not pid:
        return redirect(url_for("landing"))
    state = get_state(pid)
    if state is None:
        return redirect(url_for("landing"))
    if state["trial_idx"] >= len(state["pairings"]):
        return redirect(url_for("done"))
    return _render_trial(state)


@app.route("/submit", methods=["POST"])
def submit():
    pid = session.get("pid")
    if not pid:
        return redirect(url_for("landing"))
    state = get_state(pid)
    if state is None or state["trial_idx"] >= len(state["pairings"]):
        return redirect(url_for("done"))

    item_id, rep = state["pairings"][state["trial_idx"]]
    item = ITEM_BY_ID[item_id]
    verdict = (request.form.get("verdict") or "").strip()
    feedback = (request.form.get("feedback") or "").strip()
    try:
        confidence = max(0, min(100, int(request.form.get("confidence") or 50)))
    except ValueError:
        confidence = 50
    shown_at = session.get("trial_shown_at", time.time())

    if verdict not in ("approve", "reject", "cannot_verify"):
        return _render_trial(state, error="Please choose a judgment.")
    if verdict == "reject" and len(feedback) < MIN_FEEDBACK_CHARS:
        return _render_trial(
            state, error=f"You marked it wrong — please explain what is wrong "
                         f"(at least {MIN_FEEDBACK_CHARS} characters)."
        )

    trial_overall = r.incr(K_TRIAL_COUNTER)
    record = {
        # identifiers
        "participant_id": pid,
        "sql_knowledge": state.get("sql_knowledge", ""),
        "trial_in_session": state["trial_idx"] + 1,
        "trial_overall": trial_overall,
        "timestamp": time.time(),
        # item / ground truth
        "item_id": item["id"],
        "source_id": item.get("source_id") or "",
        "synthetic": int(bool(item.get("synthetic"))),
        "label": item.get("label", ""),
        "is_correct": int(bool(item["is_correct"])),
        "question": item["question"],
        "gold_answer": item.get("gold_answer", ""),
        "model_sql": item.get("model_sql", ""),
        "synthetic_error_type": item.get("synthetic_error_type", "") or "",
        # condition
        "representation": rep,
        "rep_text_shown": item["representations"][rep],
        # judgment
        "verdict": verdict,
        "confidence": confidence,
        "feedback": feedback,
        "correct_call": int((verdict == "reject") == (not item["is_correct"]))
                        if verdict != "cannot_verify" else "",
        "time_s": round(time.time() - shown_at, 1),
    }
    log_trial(record)

    pk = pair_key(item_id, rep)
    r.incr(K_EXPOSURE.format(pair=pk))
    pending = K_PENDING.format(pair=pk)
    if int(r.get(pending) or 0) > 0:
        r.decr(pending)

    state["trial_idx"] += 1
    if state["trial_idx"] >= len(state["pairings"]):
        state["completed"] = True
    save_state(pid, state)
    return redirect(url_for("done") if state["completed"] else url_for("trial"))


@app.route("/done")
def done():
    return render_template_string(
        DONE_HTML, css=BASE_CSS, code=PROLIFIC_COMPLETION_CODE,
        n_trials=TRIALS_PER_PARTICIPANT,
    )


# ----------------------------------------------------------------------------
# Health & admin
# ----------------------------------------------------------------------------
def _auth_ok(req):
    pw = req.args.get("password") or (req.get_json(silent=True) or {}).get("password")
    return bool(pw) and pw == ADMIN_PASSWORD


@app.route("/health")
def health():
    try:
        r.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "n_items": len(ITEMS),
        "n_pairings": len(PAIRINGS),
        "trials_logged": int(r.get(K_TRIAL_COUNTER) or 0),
    }


@app.route("/admin/state")
def admin_state():
    if not _auth_ok(request):
        return {"error": "unauthorized"}, 403
    exposures = {pair_key(i, rep): int(r.get(K_EXPOSURE.format(pair=pair_key(i, rep))) or 0)
                 for i, rep in PAIRINGS}
    pendings = {pair_key(i, rep): int(r.get(K_PENDING.format(pair=pair_key(i, rep))) or 0)
                for i, rep in PAIRINGS}
    per_rep = {rep: sum(v for k, v in exposures.items() if k.endswith("::" + rep))
               for rep in REPRESENTATIONS}
    return {
        "trials_logged": int(r.get(K_TRIAL_COUNTER) or 0),
        "n_log_entries": r.llen(K_LOG_INDEX),
        "exposure_min": min(exposures.values()) if exposures else 0,
        "exposure_max": max(exposures.values()) if exposures else 0,
        "judgments_per_representation": per_rep,
        "pairings_with_zero_submits": [k for k, v in exposures.items() if v == 0],
        "pending": {k: v for k, v in pendings.items() if v > 0},
    }


@app.route("/admin/export")
def admin_export():
    """Dump every logged trial as JSON — the primary dataset for offline analysis."""
    if not _auth_ok(request):
        return {"error": "unauthorized"}, 403
    keys = r.lrange(K_LOG_INDEX, 0, -1)
    records = [json.loads(r.get(k)) for k in keys if r.get(k)]
    return {"n": len(records), "records": records}


@app.route("/admin/reset_all", methods=["POST"])
def admin_reset_all():
    if not _auth_ok(request):
        return {"error": "unauthorized"}, 403
    deleted = 0
    for pattern in ["vw:exposure:*", "vw:pending:*", "vw:participant:*", "vw:log:*"]:
        for k in r.scan_iter(pattern):
            r.delete(k)
            deleted += 1
    r.delete(K_LOG_INDEX)
    r.delete(K_TRIAL_COUNTER)
    return {"reset": True, "keys_deleted": deleted}


if __name__ == "__main__":
    app.run(debug=True, port=5000)

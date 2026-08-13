# Verifier Experiment — WTQ Representations

Prolific data-collection app for the verification-loop paper.
Question: **can a code-illiterate human verify an AI analyst's step if it is
shown in the right representation?**

Stimuli: WikiTableQuestions items (≤10-row tables, shown in full) with the
generator model's SQL, in one of three representations per trial:
`code` (raw SQL) · `strategy` (model's own CoT summarized) · `goal_means`
(GOAL/MEANS/RESULT-SHAPE explanation). Pool mixes natural-correct,
natural-incorrect, and luna-perturbed `synthetic_incorrect` items (labeled).

No model runs online — participants judge pre-computed material, so trials are
instant. Reject-feedback is mandatory because it feeds the offline Phase-2
revision run (ΔEX when the model receives human feedback).

## Design implemented in `app.py`

- Trial universe = every item × 3 representations (**pairings**).
- **Least-exposed-first assignment over pairings** (committed + in-flight
  pending with TTL), so all pairings accumulate judgments evenly — the
  "deal out the urn, then refill" scheme.
- Per participant (`TRIALS_PER_PARTICIPANT`, default 5): representations
  **mixed within-session** (≤ `REP_CAP`=3 of any one condition), and **never
  the same item twice** in any representation.
- Response per trial: approve / reject / can't-tell + confidence 0–100 +
  free text (≥ `MIN_FEEDBACK_CHARS` when rejecting; enforced client & server
  side). The model's output/answer is **withheld** — judge the approach.
- Landing page records self-reported SQL knowledge (analysis covariate; do
  **not** exclude in-app — screen in Prolific if desired).
- Logged to **Redis** (primary, `/admin/export`) and **Qualtrics** (backup).

## Deploy (same pattern as dss-experiment-activevision)

1. New GitHub repo with these files; connect to a **new** Render web service.
2. Start command: `gunicorn app:app --threads 8 --timeout 60`.
3. Attach a Redis (Key-Value) instance. This app namespaces keys under `vw:`
   so it can share an instance, but a fresh one is cleaner.
4. Env vars: `REDIS_URL`, `ADMIN_PASSWORD`, `QUALTRICS_DATACENTER`,
   `QUALTRICS_API_TOKEN`, `QUALTRICS_SURVEY_ID`, `PROLIFIC_COMPLETION_CODE`.
   Optional: `TRIALS_PER_PARTICIPANT` (5), `REP_CAP` (3),
   `MIN_FEEDBACK_CHARS` (20), `PENDING_TTL_S` (3600).

## Launch checklist

**Before publishing on Prolific**

- [ ] Run the Colab pipeline through CELL 5 → download
      `wtq_pool_with_synthetic.json` from Drive →
      `python scripts/make_pool_data.py wtq_pool_with_synthetic.json` →
      commit & push the produced `pool_data.json`
      (the checked-in one is a 6-item PLACEHOLDER and the app logs a warning).
- [ ] Check `/health` — `n_items` and `n_pairings` (= items × 3) match the pool.
- [ ] `POST /admin/reset_all` with `{"password": "..."}` to clear test state.
- [ ] Do one full pass yourself; verify rows land in Qualtrics AND in
      `/admin/export?password=...`; verify your 5 trials had ≥2 distinct
      representations and 5 distinct items.
- [ ] Free tier spins down — hit the URL once right before launch.

**Qualtrics** — new project; add embedded-data fields matching the record keys
in `app.py`: `participant_id`, `sql_knowledge`, `trial_in_session`,
`trial_overall`, `timestamp`, `item_id`, `source_id`, `synthetic`, `label`,
`is_correct`, `question`, `gold_answer`, `model_sql`, `synthetic_error_type`,
`representation`, `rep_text_shown`, `verdict`, `confidence`, `feedback`,
`correct_call`, `time_s`. Long fields (`rep_text_shown`, `model_sql`,
`feedback`) may be truncated by Qualtrics — Redis `/admin/export` is the
authoritative dataset.

**Prolific** — new study pointing at the Render URL with
`?PROLIFIC_PID={{%PROLIFIC_PID%}}`. Places arithmetic: with I items you have
`3I` pairings; for `n` judgments per pairing you need `3·I·n / 5` participants
(e.g. 60 items, n=6 → 216 places at 5 trials each). Budget ~10 min/participant.
Optionally prescreen for "no programming experience" — but the in-app SQL
question is recorded regardless, so you can also recruit broadly and split in
analysis.

**After collection** — pull `/admin/export`, then per representation compute:
TPR = P(reject | incorrect), FPR = P(reject | correct), can't-tell rate,
confidence calibration; split incorrect into natural vs `synthetic` (different
error populations — report separately and pooled). `correct_call` is
precomputed per judgment (empty for can't-tell). Reject-feedback texts are the
Phase-2 input: feed each to the generator's revision prompt offline,
re-execute, and measure ΔEX by representation.

## Endpoints

- `/` landing (accepts `?PROLIFIC_PID=...`), `/trial`, `/submit`, `/done`
- `/health` — status, item/pairing counts, trials logged
- `/admin/state?password=…` — per-pairing exposure, per-representation totals
- `/admin/export?password=…` — all trial records (primary dataset)
- `POST /admin/reset_all` — wipe study state (irreversible)

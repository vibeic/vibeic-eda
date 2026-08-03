#!/usr/bin/env python3
"""test_assess.py — token-free unit tests for the selective-merge assessment engine.

Exercises the deterministic + combine logic without any gh/git/claude calls:
the clearly-safe gate, the stub/degraded classify normalization, and the markdown
render. Run:  python3 test_assess.py
"""
import json
import textwrap
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import assess_release as A
import gk_state as GK


# A judgement the confirmation round agreed on. Every auto-adopt assertion below has to
# carry one, because since vibeic/vibeic-eda#6 a verdict only ONE sample supports is not
# clearly-safe: three judgements of one 105-commit range returned three different useful
# sets, so the tier that opens a cherry-pick PR needs agreement, not a single reading.
AGREED = {"agree": True, "complete": True,
          "readings": [[True, "low"], [True, "low"], [True, "low"]],
          "detail": "3 independent judgements agreed (useful=true, risk=low)"}


def test_clearly_safe_gate():
    safe = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    assert A._clearly_safe(safe, False, True, None, AGREED) is True
    assert A._clearly_safe(safe, True, True, None, AGREED) is False   # overlaps our patch
    assert A._clearly_safe(safe, False, False, None, AGREED) is False  # dirty pick
    assert A._clearly_safe(safe, False, None, None, AGREED) is False   # unknown pick
    assert A._clearly_safe({**safe, "risk": "medium"}, False, True, None, AGREED) is False
    assert A._clearly_safe({**safe, "category": "feature"}, False, True, None, AGREED) is False
    assert A._clearly_safe({**safe, "relevant": False}, False, True, None, AGREED) is False
    assert A._clearly_safe({**safe, "recommend": "manual"}, False, True, None, AGREED) is False
    assert A._clearly_safe(A._not_assessed("truncated"), False, True, None, AGREED) is False
    # and the #6 condition itself: no agreement record at all, a disagreement, and an
    # incomplete round are each enough on their own to withhold auto-adopt.
    assert A._clearly_safe(safe, False, True) is False, "one sample was enough for auto-adopt"
    assert A._clearly_safe(safe, False, True, None, {**AGREED, "agree": False}) is False
    assert A._clearly_safe(safe, False, True, None, {**AGREED, "complete": False}) is False
    assert A._clearly_safe(safe, False, True, None, {}) is False
    assert A._clearly_safe(safe, False, True, None, "yes") is False   # not even a dict
    # the readings are the DISCLOSURE payload, not a gate input — an agreed, complete
    # record still admits without them
    assert A._clearly_safe(safe, False, True, None,
                           {"agree": True, "complete": True}) is True


def test_classify_stub_and_degraded_fill():
    commits = [{"sha": "aaa111", "title": "fix null deref in drc"},
               {"sha": "bbb222", "title": "add feature X"}]
    stub = {"aaa111": {"category": "bugfix", "risk": "low", "relevant": True,
                       "recommend": "adopt", "summary": "fix drc null"}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(stub, f)
        sp = f.name
    try:
        os.environ["GK_ASSESS_STUB"] = sp
        cm = A.classify_commits("magic", "DRC", commits)
        assert cm["aaa111"]["category"] == "bugfix"
        assert cm["bbb222"]["recommend"] == "manual"        # omitted sha → degraded
    finally:
        os.environ.pop("GK_ASSESS_STUB", None)
        os.unlink(sp)


def test_classify_failsafe_on_bad_stub():
    commits = [{"sha": "aaa111", "title": "x"}, {"sha": "bbb222", "title": "y"}]
    os.environ["GK_ASSESS_STUB"] = "/no/such/file.json"
    try:
        cm = A.classify_commits("magic", "DRC", commits)
        assert all(v["recommend"] == "manual" for v in cm.values())
    finally:
        os.environ.pop("GK_ASSESS_STUB", None)


def test_classify_empty():
    assert A.classify_commits("magic", "DRC", []) == {}


def test_render_md():
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.400", "latest": "8.3.675",
           "our_ref": "9f91cd24", "our_patch_files": 3, "commit_count": 2, "aggregate_files": 5,
           "clearly_safe": ["aaa111"],
           "commits": [
               {"sha": "aaa111", "title": "fix drc", "category": "bugfix", "risk": "low",
                "relevant": True, "touches_our_patches": False, "clean_cherrypick": True,
                "recommend": "adopt", "decision": "auto-safe", "summary": "fix drc null",
                "reproduce": "run drc on empty cell"},
               {"sha": "bbb222", "title": "add feature X", "category": "feature", "risk": "medium",
                "relevant": False, "touches_our_patches": None, "clean_cherrypick": None,
                "recommend": "manual", "decision": "human", "summary": ""}]}
    md = A.render_md(rep)
    assert "selective-merge assessment" in md
    assert "8.3.400 → 8.3.675" in md
    assert "auto-safe" in md and "Reproduce-before-adopt" in md
    assert "run drc on empty cell" in md


def test_render_md_clean_and_error():
    assert "nothing to assess" in A.render_md({"tool": "yosys", "status": "clean"})
    assert "assessment error" in A.render_md({"tool": "yosys", "error": "boom"})


def test_our_patch_files_unknown_on_error_fails_safe():
    # gh error → None (UNKNOWN), never set() — so the conflict gate can't read it as "no overlap"
    orig = A._gh
    try:
        A._gh = lambda path: {"_err": "403 rate limit"}
        assert A.our_patch_files("YosysHQ/yosys", "main", "deadbeef", "yosys") is None
        # and the assess() touch rule: unknown our_files (None) OR unknown commit files → touches=True
        our_files, cf = None, {"foo.cc"}
        touches = True if (our_files is None or cf is None) else bool(our_files & cf)
        assert touches is True
        assert A._clearly_safe({"category": "bugfix", "risk": "low", "relevant": True,
                                "recommend": "adopt"}, touches, True) is False   # cannot be auto-safe
    finally:
        A._gh = orig


def test_gh_never_raises():
    import subprocess as _sp
    orig = _sp.run
    try:
        _sp.run = lambda *a, **k: (_ for _ in ()).throw(OSError("gh not found"))
        r = A._gh("repos/x/y")
        assert isinstance(r, dict) and r.get("_err"), "OSError → _err, not a raise"
    finally:
        _sp.run = orig


def test_classify_maps_judge_verdicts():
    # the SAFE tool-less judge's {useful,reason,risk} maps into the assess() classify shape
    import llm_judge
    os.environ.pop("GK_ASSESS_STUB", None)
    orig = llm_judge.judge
    try:
        llm_judge.judge = lambda tool, role, commits: llm_judge.JudgeOutcome(
            {"u1": {"useful": True, "reason": "fixes DRC crash", "risk": "low"},
             "n1": {"useful": False, "reason": "CI only", "risk": "low"}}, {})
        out = A.classify_commits("magic", "DRC", [{"sha": "u1", "title": "x"}, {"sha": "n1", "title": "y"}])
        assert out["u1"]["category"] == "bugfix" and out["u1"]["recommend"] == "adopt" and out["u1"]["relevant"] is True
        assert out["n1"]["category"] == "other" and out["n1"]["recommend"] == "skip" and out["n1"]["relevant"] is False
    finally:
        llm_judge.judge = orig


def test_classify_degrades_when_judge_returns_nothing():
    import llm_judge
    os.environ.pop("GK_ASSESS_STUB", None)
    orig = llm_judge.judge
    try:
        llm_judge.judge = lambda *a, **k: llm_judge.JudgeOutcome({}, {"a": "API error"})
        out = A.classify_commits("magic", "DRC", [{"sha": "a", "title": "x"}])
        assert out["a"]["recommend"] == "manual", "no verdict → manual (never auto-adopt)"
        assert out["a"]["category"] == A.NOT_ASSESSED and out["a"]["risk"] == A.NOT_ASSESSED
        assert "API error" in out["a"]["summary"], "the reason must reach the report"
    finally:
        llm_judge.judge = orig


def test_classify_survives_a_judge_that_returns_junk():
    """A judge that raises, or returns the wrong type, must still yield a per-commit
    NOT-ASSESSED row rather than an exception or a fabricated verdict."""
    import llm_judge
    os.environ.pop("GK_ASSESS_STUB", None)
    orig = llm_judge.judge
    for bad in (lambda *a, **k: None,
                lambda *a, **k: {"a": {"useful": True, "risk": "low"}},   # legacy raw dict
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))):
        try:
            llm_judge.judge = bad
            out = A.classify_commits("magic", "DRC", [{"sha": "a", "title": "x"}])
            assert out["a"]["category"] == A.NOT_ASSESSED
            assert out["a"]["recommend"] == "manual"
        finally:
            llm_judge.judge = orig


def test_llm_judge_never_raises_without_token():
    import llm_judge
    orig = llm_judge.CRED
    try:
        llm_judge.CRED = Path("/no/such/cred.json")   # no credential file
        r = llm_judge.judge("magic", "DRC", [{"sha": "a", "title": "x"}])
        assert r.verdicts == {}
        assert "a" in r.unassessed and "NOT classified" in r.unassessed["a"]
        empty = llm_judge.judge("magic", "DRC", [])
        assert empty.verdicts == {} and empty.unassessed == {}
    finally:
        llm_judge.CRED = orig


# ── determinism / idempotency (2026-07-25) ───────────────────────────────────
# The magic range 8.3.674→8.3.676 was re-assessed 7 days running with NO upstream
# change, and the clearly-safe count oscillated 1,1,1,0,0,0,1 — a sampled `risk`
# flipping a commit between human-review and auto-adopt. Two guards below.

def _cache_fixture(tmp, risks):
    """assess() wired to stub layers; `risks` is consumed one per classify call."""
    os.environ["GK_STATE_DIR"] = str(tmp)
    import importlib
    importlib.reload(A)
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / "ledger" / "magic.json").write_text(json.dumps({
        "tool": "magic", "integrated": True, "behind_releases": 1,
        "upstream": "up/magic", "upstream_default_branch": "master",
        "pinned_ref_full": "a" * 40, "base_release": "8.3.674",
        "upstream_latest_release": "8.3.676", "role": "DRC"}))
    A.upstream_commits = lambda *a: ([{"sha": "cc4da9a05fde", "sha_full": "c" * 40,
                                       "title": "fix substrate extraction", "body": "",
                                       "url": "", "author": "x"}], ["ext.c"])
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: {"ext.c"}
    A.clean_cherrypick = lambda *a: True
    seq = list(risks)
    calls = []
    def classify(tool, role, commits):
        calls.append(1)
        r = seq.pop(0) if seq else "low"
        return {c["sha"]: {"category": "bugfix", "relevant": True, "risk": r,
                           "summary": "s", "reproduce": "", "recommend": "adopt"}
                for c in commits}
    A.classify_commits = classify
    # The confirmation round (vibeic/vibeic-eda#6) is a stubbed LAYER here, exactly like
    # `_commit_files` / `clean_cherrypick` above: these tests are about the CACHE, and
    # leaving it live would make `calls` count samples instead of assessment rounds and
    # make the `risks` sequence mean something else. The round itself is proven
    # end-to-end against the real HTTP layer in the #6 tests below.
    A._confirm_candidates = lambda tool, role, cands, cls_map: {
        c["sha"]: {"agree": True, "complete": True,
                   "readings": [[True, "low"]] * 3, "detail": "stubbed: agreed"}
        for c in cands}
    return calls


def test_unchanged_range_replays_and_does_not_redrift():
    """Identical input ⇒ stored verdict replayed, no second judgment, no drift."""
    with tempfile.TemporaryDirectory() as d:
        calls = _cache_fixture(Path(d), ["low", "medium"])   # 2nd call WOULD drift
        r1 = A.assess("magic")
        r2 = A.assess("magic")
        assert r1["clearly_safe"] == ["cc4da9a05fde"]
        assert r2["clearly_safe"] == r1["clearly_safe"], "verdict drifted on identical input"
        assert r2.get("cached") is True
        assert len(calls) == 1, f"re-judged an unchanged range ({len(calls)} calls)"


def test_new_range_is_reassessed():
    """A real upstream move must NOT be masked by the cache."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        calls = _cache_fixture(tmp, ["low", "low"])
        A.assess("magic")
        led = json.loads((tmp / "ledger" / "magic.json").read_text())
        led["upstream_latest_release"] = "8.3.677"           # upstream advanced
        (tmp / "ledger" / "magic.json").write_text(json.dumps(led))
        r = A.assess("magic")
        assert not r.get("cached"), "a NEW range was wrongly served from cache"
        assert len(calls) == 2


def test_degraded_assessment_is_not_cached():
    """A judge outage must not freeze a provisional 'needs human' verdict forever."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["GK_STATE_DIR"] = d
        import importlib
        importlib.reload(A)
        tmp = Path(d)
        _cache_fixture(tmp, [])
        A.classify_commits = lambda tool, role, commits: {
            c["sha"]: A._not_assessed("judge outage") for c in commits}
        r1 = A.assess("magic")
        assert r1["clearly_safe"] == []
        assert not r1.get("cached")
        r2 = A.assess("magic")
        assert not r2.get("cached"), "degraded (AI-unavailable) verdict was cached"


def test_judge_request_pins_temperature_zero():
    """`risk` gates auto-adopt, so the judgment must not be sampled."""
    src = (Path(__file__).parent / "llm_judge.py").read_text()
    assert '"temperature": 0' in src, "llm_judge must pin temperature=0"


# ── carried / recorded-decision filters (2026-07-25) ─────────────────────────
# magic 8.3.674->8.3.676 reported "2 need human decision" for 7 days while BOTH
# were already in what we ship: cc4da9a05fde as a direct ancestor, a22b7508acfe
# as cherry-pick fe91f011 (identical patch-id). behind_releases compares RELEASE
# TAGS but selective-merge adopts COMMITS, so the tag never advances and the
# fork reads "behind" forever, re-proposing work that is already done.

def test_already_carried_is_fail_open_without_a_clone():
    """UNKNOWN must read as NOT-carried: a genuinely new commit must never be
    silently dropped from review because we could not check."""
    orig = A.FORKS_DIR
    try:
        A.FORKS_DIR = Path("/nonexistent-fork-dir")
        got = A.already_carried("magic", "someref",
                                [{"sha": "aaa111", "sha_full": "a" * 40}])
        assert got == set()
    finally:
        A.FORKS_DIR = orig


def test_already_carried_empty_inputs():
    assert A.already_carried("magic", "ref", []) == set()
    assert A.already_carried("magic", "", [{"sha": "a", "sha_full": "a" * 40}]) == set()


def test_recorded_decisions_reads_the_register():
    d = A.recorded_decisions("magic")
    assert isinstance(d, dict)
    for sha, rec in d.items():
        assert rec.get("decision") in ("skip", "adopt"), (sha, rec)
        assert len(str(rec.get("reason", ""))) >= 20, f"{sha} has no real reason"
        assert rec.get("decided_by") and rec.get("decided_on")


def test_recorded_decisions_fail_open():
    """Unknown tool, and a missing/corrupt register, yield {} — never an
    exception, and never a decision we did not make."""
    assert A.recorded_decisions("no-such-tool-xyz") == {}
    orig = A.DECISIONS
    try:
        A.DECISIONS = Path(tempfile.mkdtemp()) / "missing.json"
        assert A.recorded_decisions("magic") == {}
        bad = Path(tempfile.mkdtemp()) / "bad.json"
        bad.write_text("{not json")
        A.DECISIONS = bad
        assert A.recorded_decisions("magic") == {}
    finally:
        A.DECISIONS = orig


def test_render_marks_a_fully_settled_range_as_decided():
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.674",
           "latest": "8.3.676", "our_patch_files": 59, "commit_count": 2,
           "carried": ["aaa111"], "decided": ["bbb222"], "clearly_safe": [],
           "outstanding": [],
           "commits": [
               {"sha": "aaa111", "category": "carried", "risk": None,
                "relevant": None, "touches_our_patches": None,
                "clean_cherrypick": None, "recommend": "carried",
                "decision": "carried", "summary": "already ours", "title": ""},
               {"sha": "bbb222", "category": "decided", "risk": None,
                "relevant": None, "touches_our_patches": None,
                "clean_cherrypick": None, "recommend": "skip",
                "decision": "recorded:skip", "summary": "UI only", "title": ""}]}
    md = A.render_md(rep)
    assert "needs human decision: 0" in md
    assert "DECIDED — no action required" in md
    assert "RELEASE TAGS" in md          # explains why "behind" is not "owed work"


# ── a truncated judge reply (vibeic/vibeic-eda#3, 2026-07-28) ────────────────
# magic 8.3.674 → 8.3.678 sent 80 commits in ONE request against a 4096-token
# output cap. The reply came back pretty-printed, hit the cap at exactly 4096
# output tokens (stop_reason=max_tokens) and was cut mid-string. `json.loads`
# raised, the handler returned None, and the caller degraded ALL 108 commits to
# category=other / relevant=None / risk=high / recommend=manual. The published
# report read "105 need human decision", every row high risk — while the
# truncated reply had in fact classified 76 of those same build/GHA commits
# `risk: low`. Nothing ever looked at `stop_reason`, so a cut-off reply was
# indistinguishable from a network error.
#
# These tests stub the HTTP layer; none of them calls the API.

class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _shas_in(req_body: dict) -> list[str]:
    """The shas one request actually asked about."""
    txt = req_body["messages"][0]["content"]
    return [ln.split()[1] for ln in txt.splitlines() if ln.startswith("- ")]


def _pretty_reply(shas, useful=False, risk="low"):
    """The pretty-printed JSON the model really emits (~3x compact size)."""
    return "{\n" + ",\n".join(
        f'  "{s}": {{\n    "useful": {"true" if useful else "false"},\n'
        f'    "reason": "GHA / packaging churn, no signoff impact",\n'
        f'    "risk": "{risk}"\n  }}' for s in shas) + "\n}"


def _stub_api(make_payload, sent=None):
    """Replace the HTTP layer llm_judge uses. `make_payload(shas) -> API response.`"""
    def fake(req, timeout=None):
        body = json.loads(req.data.decode())
        if sent is not None:
            sent.append(body)
        return _FakeResp(make_payload(_shas_in(body)))
    return fake


def _with_stubbed_api(make_payload, fn, sent=None):
    import urllib.request as U
    import llm_judge
    orig_tok, orig_open = llm_judge._token, U.urlopen
    try:
        llm_judge._token = lambda: "stub-token"
        U.urlopen = _stub_api(make_payload, sent)
        return fn()
    finally:
        llm_judge._token, U.urlopen = orig_tok, orig_open


def _truncated_payload(shas, keep=3):
    """A reply cut mid-string inside entry #keep+1 — exactly the observed failure."""
    full = _pretty_reply(shas)
    cut = full.index(f'"{shas[keep]}"') + 30      # lands inside `"useful": fal…`
    return {"content": [{"type": "text", "text": full[:cut]}],
            "stop_reason": "max_tokens", "usage": {"output_tokens": 4096}}


def test_truncated_reply_is_disclosed_and_partial_results_survive():
    """HALF ONE: a reply cut mid-JSON yields a DISCLOSED not-assessed outcome for the
    shas that were actually lost — and keeps every judgment that arrived intact."""
    import llm_judge
    commits = [{"sha": f"sha{i:03d}", "title": "GHA bump"} for i in range(5)]
    r = _with_stubbed_api(lambda shas: _truncated_payload(shas, keep=3),
                          lambda: llm_judge.judge("magic", "DRC", commits))
    # partial survival — the old code discarded all five
    assert len(r.verdicts) == 3, f"completed judgments were discarded: {r.verdicts}"
    assert all(v["risk"] == "low" for v in r.verdicts.values())
    # and ONLY the genuinely missing shas are unassessed
    assert set(r.unassessed) == {"sha003", "sha004"}
    assert all("max_tokens" in w for w in r.unassessed.values()), r.unassessed
    # every input sha lands in exactly one of the two maps
    assert set(r.verdicts) | set(r.unassessed) == {c["sha"] for c in commits}
    assert not (set(r.verdicts) & set(r.unassessed))


def test_wellformed_reply_still_classifies():
    """HALF TWO: the fix must not cost the happy path. A complete reply classifies."""
    import llm_judge
    commits = [{"sha": f"sha{i:03d}", "title": "fix DRC crash"} for i in range(4)]
    r = _with_stubbed_api(
        lambda shas: {"content": [{"type": "text", "text": _pretty_reply(shas, useful=True)}],
                      "stop_reason": "end_turn"},
        lambda: llm_judge.judge("magic", "DRC", commits))
    assert r.unassessed == {}, r.unassessed
    assert len(r.verdicts) == 4
    assert all(v["useful"] is True and v["risk"] == "low" for v in r.verdicts.values())


def test_judge_covers_the_whole_range_in_output_sized_chunks():
    """The old code sent commits[:80] in ONE request: on the 105-commit magic range 25
    commits were never even asked about, and the 80 that were could not fit the cap."""
    import llm_judge
    import math
    n = 100
    commits = [{"sha": f"sha{i:03d}", "title": "t"} for i in range(n)]
    sent = []
    r = _with_stubbed_api(
        lambda shas: {"content": [{"type": "text", "text": _pretty_reply(shas)}],
                      "stop_reason": "end_turn"},
        lambda: llm_judge.judge("magic", "DRC", commits), sent=sent)
    assert set(r.verdicts) == {c["sha"] for c in commits}, "the range was truncated"
    assert r.unassessed == {}
    assert len(sent) == math.ceil(n / llm_judge.CHUNK), f"{len(sent)} requests"
    assert max(len(_shas_in(b)) for b in sent) <= llm_judge.CHUNK
    assert all(b["max_tokens"] == llm_judge.MAX_TOKENS for b in sent)
    assert all(b["temperature"] == 0 for b in sent), "the verdict must not be sampled"


def test_a_failing_chunk_costs_only_its_own_commits():
    """Chunk independence: one bad reply must not discard the other chunks' judgments."""
    import llm_judge
    commits = [{"sha": f"sha{i:03d}", "title": "t"} for i in range(2 * llm_judge.CHUNK)]
    calls = []

    def payload(shas):
        calls.append(1)
        if len(calls) == 1:
            return {"content": [{"type": "text", "text": "not json at all"}],
                    "stop_reason": "end_turn"}
        return {"content": [{"type": "text", "text": _pretty_reply(shas)}],
                "stop_reason": "end_turn"}

    r = _with_stubbed_api(payload, lambda: llm_judge.judge("magic", "DRC", commits))
    assert len(r.verdicts) == llm_judge.CHUNK, "a bad chunk took the good one down"
    assert len(r.unassessed) == llm_judge.CHUNK
    assert all("NOT classified" in w for w in r.unassessed.values())


def test_chunk_size_is_derived_from_the_measured_output_cost():
    """The chunk size must come from the OUTPUT budget, not from a commit count."""
    import llm_judge
    assert llm_judge.CHUNK == llm_judge.MAX_TOKENS // llm_judge._OUT_TOKENS_PER_COMMIT
    # measured 2026-07-28 on magic 8.3.674→8.3.678: 4096 output tokens produced exactly
    # 77 complete entries before the cut → 53.2 output tokens per commit.
    measured = 4096 / 77
    assert llm_judge.CHUNK * measured < 0.6 * llm_judge.MAX_TOKENS, "no headroom"
    assert 80 * measured > llm_judge.MAX_TOKENS, "the old 80-commit request could not fit"


def test_salvage_keeps_complete_entries_and_never_invents_one():
    import llm_judge as J
    good = '{"a": {"useful": true, "reason": "x", "risk": "low"}}'
    assert J._salvage_json_object(good) == json.loads(good)
    assert J._salvage_json_object("chatty preamble " + good + " trailer") == json.loads(good)
    assert J._salvage_json_object('{"a": {"useful": true, "reas') is None   # nothing complete
    assert J._salvage_json_object("no json here") is None
    assert J._salvage_json_object("") is None
    # braces and escaped quotes INSIDE a string must not fool the scanner
    tricky = '{"a": {"useful": true, "reason": "x } { \\" y", "risk": "low"}, "b": {"use'
    assert J._salvage_json_object(tricky) == {
        "a": {"useful": True, "reason": 'x } { " y', "risk": "low"}}


# ── a degraded row must not look like a judgement ────────────────────────────
def _clearly_safe_BEFORE(cls, touches_our_files, clean_pick):
    """FROZEN copy of the gate as it stood at 84b2a7f — the baseline for the
    strictness proof below. Do not 'fix' this to match the live one."""
    return (cls.get("category") == "bugfix"
            and cls.get("risk") == "low"
            and cls.get("relevant") is True
            and cls.get("recommend") == "adopt"
            and not touches_our_files
            and clean_pick is True)


def test_clearly_safe_is_no_looser_than_before():
    """EXHAUSTIVE over the gate's whole input domain: every input the NEW gate calls
    auto-adoptable, the OLD gate called auto-adoptable too. Retiring the `risk="high"`
    default must not open a door — including via the new `not-assessed` token."""
    import itertools
    cats = ["bugfix", "other", "feature", A.NOT_ASSESSED, None]
    risks = ["low", "medium", "high", A.NOT_ASSESSED, None]
    rels = [True, False, None]
    recs = ["adopt", "skip", "manual", None]
    tri = [True, False, None]
    n = looser = 0
    for cat, risk, rel, rec, t, cp in itertools.product(cats, risks, rels, recs, tri, tri):
        cls = {"category": cat, "risk": risk, "relevant": rel, "recommend": rec}
        n += 1
        if A._clearly_safe(cls, t, cp) and not _clearly_safe_BEFORE(cls, t, cp):
            looser += 1
    assert n == 5 * 5 * 3 * 4 * 3 * 3 == 2700
    assert looser == 0, f"{looser}/{n} inputs became newly auto-adoptable"
    # and a not-assessed row can never be auto-adopted, under ANY probe outcome
    for t, cp in itertools.product(tri, tri):
        assert A._clearly_safe(A._not_assessed("truncated"), t, cp) is False


def test_not_assessed_row_is_not_a_judgement():
    na = A._not_assessed("the judge's reply hit the output-token cap")
    # THE regression: `high` in the column a reviewer triages on was a fabricated finding
    assert na["risk"] != "high"
    assert na["risk"] not in ("low", "medium", "high"), "an absence must not read as a verdict"
    assert na["risk"] == A.NOT_ASSESSED and na["category"] == A.NOT_ASSESSED
    assert na["relevant"] is None
    assert "NOT ASSESSED" in na["summary"] and "output-token cap" in na["summary"]
    assert na["recommend"] == "manual", "manual is still the correct ACTION"
    assert na["_note"], "must keep the assessment out of the cache"


def test_render_discloses_an_incomplete_judgement():
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.674",
           "latest": "8.3.678", "our_patch_files": 59, "commit_count": 2,
           "carried": [], "decided": [], "clearly_safe": [], "outstanding": ["bbb222"],
           "not_assessed": ["bbb222"],
           "commits": [
               {"sha": "aaa111", "title": "fix drc", "category": "bugfix", "risk": "low",
                "relevant": True, "touches_our_patches": False, "clean_cherrypick": True,
                "recommend": "adopt", "decision": "human", "summary": "real judgement"},
               {**A._not_assessed("reply truncated at the output cap"), "sha": "bbb222",
                "title": "GHA bump", "touches_our_patches": None,
                "clean_cherrypick": None, "decision": "human"}]}
    md = A.render_md(rep)
    assert "THE JUDGE DID NOT COMPLETE" in md
    assert "1 of 2 commit(s) were NOT ASSESSED" in md
    assert "not-probed" in md, "an unrun probe must not render as a neutral dash"
    assert "| high |" not in md, "no fabricated risk verdict"
    assert "DID NOT RUN" in md, "must say the conflict/clean-pick analyses did not run"
    assert "real judgement" in md          # the assessed row still renders normally
    # the banner promises cat/risk/rel all read `not-assessed` — the row must agree,
    # or the disclosure describes a table the reader is not looking at
    row = next(ln for ln in md.splitlines() if ln.startswith("| `bbb222`"))
    cells = [x.strip() for x in row.split("|")]
    assert cells[2] == cells[3] == cells[4] == A.NOT_ASSESSED, row
    assert cells[5] == cells[6] == "not-probed", row
    # the reason must survive the summary-column truncation intact
    assert "reply truncated at the output cap" in md


def test_render_settled_range_is_unaffected_by_the_disclosure_banner():
    """A fully-decided range must NOT grow a scary banner: n_assessed there is 0."""
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.674",
           "latest": "8.3.676", "our_patch_files": 59, "commit_count": 1,
           "carried": ["aaa111"], "decided": [], "clearly_safe": [], "outstanding": [],
           "not_assessed": [],
           "commits": [{"sha": "aaa111", "category": "carried", "risk": None,
                        "relevant": None, "touches_our_patches": None,
                        "clean_cherrypick": None, "recommend": "carried",
                        "decision": "carried", "summary": "already ours", "title": ""}]}
    md = A.render_md(rep)
    assert "THE JUDGE DID NOT COMPLETE" not in md
    assert "DECIDED — no action required" in md
    assert "n/a" in md, "a settled row's probe columns are not-applicable, not unknown"


def test_assess_end_to_end_discloses_truncation_and_keeps_partials():
    """The whole path — truncated API reply → judge → classify → assess → render."""
    import importlib
    import llm_judge
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["GK_STATE_DIR"] = str(tmp)
        os.environ.pop("GK_ASSESS_STUB", None)
        try:
            importlib.reload(A)
            (tmp / "ledger").mkdir(parents=True, exist_ok=True)
            (tmp / "ledger" / "magic.json").write_text(json.dumps({
                "tool": "magic", "integrated": True, "behind_releases": 1,
                "upstream": "up/magic", "upstream_default_branch": "master",
                "pinned_ref_full": "", "base_release": "8.3.674",
                "upstream_latest_release": "8.3.678", "role": "DRC"}))
            cs = [{"sha": f"sha{i:03d}", "sha_full": f"{i:040d}", "title": "GHA bump",
                   "body": "", "url": "", "author": "x"} for i in range(5)]
            A.upstream_commits = lambda *a: (cs, ["f.c"])
            A.our_patch_files = lambda *a: set()
            A._commit_files = lambda *a: set()
            A.clean_cherrypick = lambda *a: True
            rep = _with_stubbed_api(lambda shas: _truncated_payload(shas, keep=3),
                                    lambda: A.assess("magic"))
        finally:
            os.environ.pop("GK_STATE_DIR", None)
    # only the shas actually lost are degraded — NOT all five
    assert rep["not_assessed"] == ["sha003", "sha004"], rep["not_assessed"]
    # the three that completed carry their REAL verdict, not a default
    kept = [c for c in rep["commits"] if c["category"] != A.NOT_ASSESSED]
    assert len(kept) == 3 and all(c["risk"] == "low" for c in kept)
    assert not any(c.get("risk") == "high" for c in rep["commits"]), \
        "a fabricated high-risk verdict was published"
    assert rep["clearly_safe"] == [], "an unassessed range must never auto-adopt"
    md = A.render_md(rep)
    assert "THE JUDGE DID NOT COMPLETE" in md and "2 of 5" in md


def test_incomplete_assessment_is_not_cached():
    """A truncated run must stay provisional so the next tick can re-resolve it."""
    import importlib
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        os.environ["GK_STATE_DIR"] = str(tmp)
        os.environ.pop("GK_ASSESS_STUB", None)
        try:
            importlib.reload(A)
            (tmp / "ledger").mkdir(parents=True, exist_ok=True)
            (tmp / "ledger" / "magic.json").write_text(json.dumps({
                "tool": "magic", "integrated": True, "behind_releases": 1,
                "upstream": "up/magic", "upstream_default_branch": "master",
                "pinned_ref_full": "", "base_release": "8.3.674",
                "upstream_latest_release": "8.3.678", "role": "DRC"}))
            cs = [{"sha": f"sha{i:03d}", "sha_full": f"{i:040d}", "title": "t",
                   "body": "", "url": "", "author": "x"} for i in range(5)]
            A.upstream_commits = lambda *a: (cs, ["f.c"])
            A.our_patch_files = lambda *a: set()
            A._commit_files = lambda *a: set()
            A.clean_cherrypick = lambda *a: True
            run = lambda: _with_stubbed_api(                      # noqa: E731
                lambda shas: _truncated_payload(shas, keep=3), lambda: A.assess("magic"))
            run()
            assert not run().get("cached"), "a truncated verdict was frozen into the cache"
        finally:
            os.environ.pop("GK_STATE_DIR", None)


def test_gatekeeper_and_pr_body_report_the_incomplete_judgement():
    """The disclosure has to reach the two places a human actually reads.

    Driven through the real note builder and the real tally builder. This test used to
    grep both source files for `a.get("not_assessed")` — which passes whether or not the
    string reaches a reader, and fails on a rename that changes nothing.
    """
    rep = _rep(commit_count=4, clearly_safe=[], carried=[], decided=[],
               outstanding=["a", "b", "c", "d"], not_assessed=["c", "d"])
    note = _gk().assessment_entry(rep, 1, "v2")["note"]
    assert "NOT ASSESSED" in note and "2 commit(s) NOT ASSESSED" in note
    line = _pn().tally_line("magic", rep)
    assert "NOT ASSESSED" in line and "2 commit(s) NOT" in line


# ── the summary must CONSUME the assessment's classification (vibe-ic#369) ──
# `assess_release` resolves three categories: clearly-safe, CARRIED (ancestry
# or cherry-pick patch-id) and DECIDED (recorded gatekeeper decision). The
# sync-log note re-derived "needs human" as `commit_count - clearly_safe`,
# discarding the last two. Measured on magic 8.3.674 -> 8.3.676: 2 carried +
# 1 recorded skip = nothing outstanding, yet it reported "3 need human
# review" and the settled range was re-proposed on 07-23, 07-24 and 07-26.

def _load(name):
    """Import one of the sibling modules by path, fresh, without a package."""
    import importlib.util
    import sys as _s
    from pathlib import Path as _P
    spec = importlib.util.spec_from_file_location(
        f"_{name}", _P(__file__).resolve().parent / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    _s.modules[f"_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _gk():
    return _load("gatekeeper")


def _pn():
    return _load("pr_notify")


def _rep(**kw):
    """A synthetic assessment report — the shape assess() returns."""
    base = {"tool": "magic", "status": "assessed", "base_release": "v1", "latest": "v2",
            "commit_count": 0, "clearly_safe": [], "carried": [], "decided": [],
            "outstanding": [], "commits": []}
    return {**base, **kw}


def test_369_carried_and_decided_are_not_counted_as_open_work():
    """2 carried + 1 recorded skip + nothing else = nothing outstanding, and the note
    must say so rather than re-deriving `commit_count - clearly_safe` = 3."""
    entry = _gk().assessment_entry(
        _rep(commit_count=3, carried=["a", "b"], decided=["c"], outstanding=[]), 2, "8.3.676")
    assert entry["verdict"] == "RESOLVED", entry
    assert "nothing outstanding" in entry["note"]
    assert entry["assessed"] == {**entry["assessed"], "carried": 2, "decided": 1,
                                 "outstanding": 0, "clearly_safe": 0}


def test_369_unknown_outstanding_still_reads_as_needing_review():
    """A report carrying no `outstanding` key at all must never read as 'nothing to
    do' — and, per vibeic/vibeic-eda#7, must first look for the answer in the ROWS
    before it resorts to arithmetic."""
    rows = [{"decision": "human", "recommend": "manual"},
            {"decision": "human", "recommend": "skip"},
            {"decision": "carried"}]
    rep = _rep(commit_count=3, carried=["a"], commits=rows)
    rep.pop("outstanding")
    n = A.summary_counts(rep)
    # BOTH `human` rows count. This asserted 1, excluding the `recommend: "skip"`
    # one — in a test whose own docstring says a report must never read as
    # "nothing to do". The assessor's recommendation is not a decision, and the
    # decision column on that row says `human`.
    assert n["outstanding"] == 2, "both rows are marked human, so both need a decision"
    assert n["derived"] == [], "an exact recount must not be reported as inferred"
    # and with NEITHER the list nor the rows, the arithmetic that remains errs toward
    # review: 5 commits, 1 safe, 1 carried, 1 decided -> 2 open, never 0.
    bare = _rep(commit_count=5, clearly_safe=["s"], carried=["c"], decided=["d"], commits=[])
    bare.pop("outstanding")
    n2 = A.summary_counts(bare)
    assert n2["outstanding"] == 2 and "outstanding" in n2["derived"], n2
    assert "INFERRED" in A.render_md(bare), "an inferred count must say it was inferred"


def test_369_nothing_outstanding_is_not_reported_as_deferred():
    """DEFERRED on a fully-resolved range is what turned settled work into a
    recurring proposal."""
    entry = _gk().assessment_entry(
        _rep(commit_count=2, carried=["a"], decided=["b"]), 1, "v2")
    assert entry["verdict"] == "RESOLVED"
    # ...and a range with a clearly-safe commit is NOT resolved: there is work to offer.
    entry2 = _gk().assessment_entry(
        _rep(commit_count=2, carried=["a"], clearly_safe=["b"]), 1, "v2")
    assert entry2["verdict"] == "DEFERRED", entry2


# ── the cache must identify the ASSESSOR too (vibeic/vibeic-eda#4) ───────────
# `_cache_key` identified the assessment's INPUT (tool, upstream range, our
# carried-patch ref) and nothing about the classifier. f312813 changed what the
# judge concludes about identical commits, and for every range already cached
# that repair was invisible: the tick printed "unchanged range — replayed from
# cache" and never called the new code. A replayed verdict and a freshly computed
# one rendered identically in the report, the PR body and the log.

def _pop_state_dir():
    """`_cache_fixture` sets GK_STATE_DIR and never clears it, and a test that
    re-points ASSESSOR_SOURCES must not leak that at the next test either (under
    pytest nothing reloads the module between tests — vibe-ic#395's lesson)."""
    os.environ.pop("GK_STATE_DIR", None)
    import importlib
    importlib.reload(A)


def test_cache_key_carries_the_assessor():
    k1 = A._cache_key("magic", "8.3.674", "8.3.678", "d" * 40, "aaaa")
    k2 = A._cache_key("magic", "8.3.674", "8.3.678", "d" * 40, "bbbb")
    assert k1 != k2, "two different judges shared one cache slot"
    assert k1.endswith("|aaaa")
    assert k1.startswith(A._cache_input_prefix("magic", "8.3.674", "8.3.678", "d" * 40) + "|")


def test_an_unchanged_assessor_still_hits_the_cache():
    """HALF TWO: widening the key must not cost the idempotency #4 keeps."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            calls = _cache_fixture(tmp, ["low", "medium"])   # a 2nd call WOULD drift
            judge = tmp / "judge_source.py"
            judge.write_text("VERDICT = 'v1'\n")
            A.ASSESSOR_SOURCES = (judge,)
            r1 = A.assess("magic")
            r2 = A.assess("magic")
            assert r2.get("cached") is True, "an unchanged assessor missed the cache"
            assert len(calls) == 1, f"re-judged an unchanged range ({len(calls)} calls)"
            assert r2["clearly_safe"] == r1["clearly_safe"]
            assert r2["assessor"] == r1["assessor"]
            assert not r2.get("reassessed_because")
        finally:
            _pop_state_dir()


def test_a_changed_assessor_misses_the_cache_on_an_unchanged_range():
    """HALF ONE — THE LOAD-BEARING HALF. Same tool, same upstream range, same
    carried-patch ref, DIFFERENT judge ⇒ the stored verdict must NOT be replayed."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            calls = _cache_fixture(tmp, ["low", "low"])
            judge = tmp / "judge_source.py"
            judge.write_text("VERDICT = 'v1'\n")
            A.ASSESSOR_SOURCES = (judge,)
            r1 = A.assess("magic")
            assert not r1.get("cached") and len(calls) == 1
            assert A.assess("magic").get("cached") is True      # settles into the cache
            assert len(calls) == 1

            # the judge changes; NOTHING about the input does (this is f312813)
            judge.write_text("VERDICT = 'v2'  # the judge now concludes differently\n")
            r3 = A.assess("magic")

            assert not r3.get("cached"), "a stale verdict replayed after the judge changed"
            assert len(calls) == 2, "the CHANGED judge was never called"
            assert r3["assessor"] != r1["assessor"]
            why = r3.get("reassessed_because", "")
            assert "assessor changed" in why, why
            assert r1["assessor"] in why and r3["assessor"] in why, why
        finally:
            _pop_state_dir()


def test_assessor_id_is_derived_from_the_judge_source_not_declared():
    """A hand-maintained version integer drifts from the code it claims to describe;
    the identity has to be the CONTENT."""
    import shutil
    real = Path(__file__).resolve().parent / "llm_judge.py"
    orig = A.ASSESSOR_SOURCES
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "llm_judge.py"
        shutil.copy(real, copy)
        try:
            A.ASSESSOR_SOURCES = (copy,)
            before = A.assessor_id()
            copy.write_text(copy.read_text() + "\n# an edit to what the judge concludes\n")
            after = A.assessor_id()
        finally:
            A.ASSESSOR_SOURCES = orig
    assert before != after, "the assessor id did not follow the judge module's content"
    assert any(p.name == "llm_judge.py" for p in A.ASSESSOR_SOURCES), \
        "the judge module is not part of the assessor identity"
    import re
    src = (Path(__file__).resolve().parent / "assess_release.py").read_text()
    assert re.search(r"^\s*ASSESSOR_VERSION\s*=", src, re.M) is None, \
        "a hand-maintained version integer came back"


def test_assessor_id_survives_an_unreadable_source():
    """Never raises, and 'I could not read the judge' must not collide with a real one."""
    orig = A.ASSESSOR_SOURCES
    try:
        A.ASSESSOR_SOURCES = (Path("/no/such/judge.py"),)
        missing = A.assessor_id()
    finally:
        A.ASSESSOR_SOURCES = orig
    assert isinstance(missing, str) and missing
    assert missing != A.assessor_id()


def test_assessor_id_tracks_the_model_and_the_chunking():
    """Both are env-overridable, so identical source can still be two judges."""
    import importlib
    import llm_judge
    base = A.assessor_id()
    for var, val in (("GK_JUDGE_MODEL", "some-other-model"), ("GK_JUDGE_CHUNK", "7")):
        try:
            os.environ[var] = val
            importlib.reload(llm_judge)
            assert A.assessor_id() != base, f"{var} did not move the assessor id"
        finally:
            os.environ.pop(var, None)
            importlib.reload(llm_judge)
    assert A.assessor_id() == base, "the assessor id did not come back"


def test_assessor_id_tracks_the_system_prompt():
    import llm_judge
    orig = llm_judge._SYS_TASK
    base = A.assessor_id()
    try:
        llm_judge._SYS_TASK = orig + " Prefer commits that touch the router."
        assert A.assessor_id() != base, "the system prompt is not part of the assessor identity"
    finally:
        llm_judge._SYS_TASK = orig
    assert A.assessor_id() == base


def _provenance_rep(**kw):
    rep = {"tool": "magic", "status": "assessed", "base_release": "8.3.674",
           "latest": "8.3.678", "our_patch_files": 59, "commit_count": 1,
           "carried": [], "decided": [], "clearly_safe": [], "outstanding": ["aaa111"],
           "not_assessed": [],
           "commits": [{"sha": "aaa111", "title": "fix drc", "category": "bugfix",
                        "risk": "low", "relevant": True, "touches_our_patches": False,
                        "clean_cherrypick": True, "recommend": "adopt",
                        "decision": "human", "summary": "real judgement"}]}
    rep.update(kw)
    return rep


def test_a_replayed_report_is_visibly_a_replay():
    """The reader of 2026-07-28-magic.md must be able to tell whether that judgement
    was computed today or restored, and against which assessor."""
    fresh = A.render_md(_provenance_rep(assessor="abc123abc123",
                                        assessed_at="2026-07-28T05:00:00Z"))
    assert "REPLAYED FROM CACHE" not in fresh
    assert "Computed on 2026-07-28T05:00:00Z" in fresh
    assert "abc123abc123" in fresh

    replay = A.render_md(_provenance_rep(assessor="abc123abc123",
                                         assessed_at="2026-07-25T05:00:00Z",
                                         cached=True, replayed_at="2026-07-28T05:00:00Z"))
    assert "REPLAYED FROM CACHE" in replay
    assert "no classifier ran" in replay
    assert "2026-07-25T05:00:00Z" in replay, "the report must say WHEN it was decided"
    assert "2026-07-28T05:00:00Z" in replay, "and when it was restored"
    assert "abc123abc123" in replay, "and by which assessor"

    old = A.render_md(_provenance_rep(cached=True))
    assert "predates assessor pinning" in old, "an unattributable replay must say so"


def test_a_legacy_input_only_cache_entry_is_rejudged_with_a_reason():
    """The live cache at f312813 held input-only keys (`magic|8.3.674|8.3.678|1918…`).
    Those must NOT replay, and the tick must say why it is spending the calls."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            calls = _cache_fixture(tmp, ["low"])
            legacy = A._cache_input_prefix("magic", "8.3.674", "8.3.676", "a" * 40)
            (tmp / "assessment-cache").mkdir(parents=True, exist_ok=True)
            (tmp / "assessment-cache" / "magic.json").write_text(json.dumps(
                {legacy: {"tool": "magic", "status": "assessed", "commit_count": 1,
                          "clearly_safe": ["a-stale-verdict"], "commits": []}}))
            r = A.assess("magic")
            assert not r.get("cached"), "a pre-assessor cache entry was replayed"
            assert r["clearly_safe"] == ["cc4da9a05fde"], "the stale verdict was served"
            assert len(calls) == 1, "the judge was not re-run"
            why = r.get("reassessed_because", "")
            assert "before the assessor was part of the cache identity" in why, why
        finally:
            _pop_state_dir()


def test_gatekeeper_log_explains_the_invalidation_spike():
    """Widening the key re-judges every cached range once. An unexplained spike in API
    calls is how a correct invalidation gets mistaken for a bug and reverted."""
    gk = (Path(__file__).resolve().parent / "gatekeeper.py").read_text()
    assert 'r.get("reassessed_because")' in gk
    assert "RE-JUDGED" in gk
    assert "unchanged assessor" in gk, "the replay log must name what it checked"


# ── the doctrine's confirmation step, as a program (vibeic/vibeic-eda#5) ─────
# Every assessment printed "confirm each bugfix reproduces in OUR version" and
# nothing implemented it. On magic 8.3.674 → 8.3.678 the ONE clearly-safe row was
# 3f1747b1fb91, whose reason read "critical for automated batch DRC/extraction
# runs" — while the patch guards CmdCrosshair()/DBWSetCrosshair(), reachable only
# from the `crosshair` command, which we never issue. The verdict was defensible;
# the EVIDENCE was not, and the evidence is what travels into a merge PR.
#
# These tests build a synthetic clone and run the REAL registrar regex, caller
# walk and surface scan over it. Nothing here touches the network.

def _run(*args, cwd):
    import subprocess
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True)


def _reach_fixture(tmp: Path):
    """A miniature C tool + its command table + our emitter tree.

    Layout mirrors the real defect: `crosshairPos()` is reachable only from the
    `crosshair` command; `drcHelper()` only from `drc`, two calls up. Our emitters
    issue `drc`, never `crosshair`.
    """
    clone = tmp / "clone"
    (clone / "commands").mkdir(parents=True)
    (clone / "dbwind").mkdir(parents=True)
    (clone / "drc").mkdir(parents=True)
    (clone / "commands" / "CmdCD.c").write_text(
        "#include <stdio.h>\n"
        "\n"
        "void\n"
        "CmdCrosshair(w, cmd)\n"
        "{\n"
        "    crosshairPos(w);\n"
        "}\n"
        "\n"
        "void\n"
        "CmdDrc(w, cmd)\n"
        "{\n"
        "    DRCBasicCheck(w);\n"
        "}\n")
    (clone / "dbwind" / "DBWtools.c").write_text(
        "void\n"
        "crosshairPos(window)\n"
        "{\n"
        "    return;\n"
        "}\n")
    (clone / "drc" / "DRCbasic.c").write_text(
        "void\n"
        "DRCBasicCheck(w)\n"
        "{\n"
        "    drcHelper();\n"
        "}\n"
        "\n"
        "void\n"
        "drcHelper()\n"
        "{\n"
        "    return;\n"
        "}\n")
    (clone / "dbwind" / "DBWcommands.c").write_text(
        "void DBWCommandInit()\n"
        "{\n"
        "    WindAddCommand(DBWclientID,\n"
        '\t"crosshair x y | off\tenable and move or disable the screen crosshair",\n'
        "\tCmdCrosshair, FALSE);\n"
        "    WindAddCommand(DBWclientID,\n"
        '\t"drc option\t\tdesign rule checker",\n'
        "\tCmdDrc, FALSE);\n"
        "}\n")
    _run("git", "init", "-q", ".", cwd=clone)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _run("git", "config", k, v, cwd=clone)
    _run("git", "add", "commands/CmdCD.c", "dbwind/DBWtools.c", "dbwind/DBWcommands.c",
         "drc/DRCbasic.c", cwd=clone)
    _run("git", "commit", "-q", "-m", "base", cwd=clone)

    def _commit(path: str, old: str, new: str, msg: str) -> str:
        p = clone / path
        p.write_text(p.read_text().replace(old, new))
        _run("git", "add", path, cwd=clone)
        _run("git", "commit", "-q", "-m", msg, cwd=clone)
        return _run("git", "rev-parse", "HEAD", cwd=clone).stdout.strip()

    crosshair_sha = _commit("dbwind/DBWtools.c", "    return;\n", "    assert(window);\n",
                            "crosshair: prevent crash in headless mode")
    drc_sha = _commit("drc/DRCbasic.c", "void\ndrcHelper()\n{\n    return;\n}",
                      "void\ndrcHelper()\n{\n    assert(1);\n}",
                      "drc: fix a check")

    emitters = tmp / "emitters"
    emitters.mkdir()
    (emitters / "magic_emit.py").write_text(
        'CMD = "magic -noconsole -dnull -rcfile x.magicrc"\n'
        'def emit():\n'
        '    return ["drc check", "drc catchup"]\n')
    return clone, [emitters], crosshair_sha, drc_sha


def test_reachability_demotes_a_commit_nothing_we_run_can_reach():
    """DIRECTION ONE: a commit whose only route in is a command we never issue."""
    import reachability as R
    with tempfile.TemporaryDirectory() as d:
        clone, roots, crosshair_sha, _ = _reach_fixture(Path(d))
        r = R.check("magic", crosshair_sha, clone=clone, roots=roots)
    assert r["verdict"] == R.UNREACHABLE, r
    assert r["commands"] == ["crosshair"], r
    assert r["surface"] == ["drc"], r
    assert r["closure_complete"] is True
    assert "crosshair" in r["detail"] and "we never issue" in r["detail"]
    # and it can never be auto-adopted, however good the model's verdict was
    good = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    assert A._clearly_safe(good, False, True, None, AGREED) is True   # without the check
    assert A._clearly_safe(good, False, True, r, AGREED) is False      # with it


def test_reachability_keeps_a_commit_our_commands_do_reach():
    """DIRECTION TWO: a drc-path commit two calls below the handler stays a candidate."""
    import reachability as R
    with tempfile.TemporaryDirectory() as d:
        clone, roots, _, drc_sha = _reach_fixture(Path(d))
        r = R.check("magic", drc_sha, clone=clone, roots=roots)
    assert r["verdict"] == R.REACHABLE, r
    assert "drc" in r["commands"], r
    assert "drc" in r["detail"]
    good = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    assert A._clearly_safe(good, False, True, r, AGREED) is True, "a reachable fix was demoted"


def test_could_not_determine_is_not_unreachable():
    """REQUIREMENT 3. Every undecidable case leaves the model's verdict standing —
    silently demoting all of them would be the same error class this fixes."""
    import reachability as R
    good = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    with tempfile.TemporaryDirectory() as d:
        clone, roots, crosshair_sha, _ = _reach_fixture(Path(d))
        cases = {
            # a tool whose command registration idiom we do not know (klayout, netgen, …)
            "no registry": R.check("some-other-tool", crosshair_sha, clone=clone, roots=roots),
            # the emitter trees are not on this machine
            "no emitters": R.check("magic", crosshair_sha, clone=clone,
                                   roots=[Path(d) / "nope"]),
            # the commit is not in the local clone
            "unknown sha": R.check("magic", "0" * 40, clone=clone, roots=roots),
            # no clone at all
            "no clone": R.check("magic", crosshair_sha, clone=Path(d) / "missing",
                                roots=roots),
        }
    for name, r in cases.items():
        assert r["verdict"] == R.UNKNOWN, f"{name}: {r}"
        assert r["detail"].startswith("NOT DETERMINED"), f"{name}: {r}"
        assert A._clearly_safe(good, False, True, r, AGREED) is True, \
            f"{name}: an undetermined surface demoted a candidate"


def test_reachability_kill_switch_is_undetermined_not_unreachable():
    import reachability as R
    os.environ["GK_REACHABILITY"] = "0"
    try:
        r = R.check("magic", "0" * 40)
    finally:
        os.environ.pop("GK_REACHABILITY", None)
    assert r["verdict"] == R.UNKNOWN and "switched off" in r["detail"]


def test_reachability_never_raises():
    import reachability as R
    orig = R.command_registry
    try:
        R.command_registry = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r = R.check("magic", "0" * 40)
    finally:
        R.command_registry = orig
    assert r["verdict"] == R.UNKNOWN and "errored" in r["detail"]
    # and the assess()-side wrapper swallows an unimportable module the same way
    assert A._reachability("magic", "") is None or isinstance(A._reachability("magic", ""), dict)


def test_surface_is_read_from_the_emitters_not_hand_listed():
    """The surface is the intersection of the TOOL's own command vocabulary with the
    command lines our emitters write — so it cannot rot into a stale hand-kept list."""
    import re
    import reachability as R
    with tempfile.TemporaryDirectory() as d:
        clone, roots, _, _ = _reach_fixture(Path(d))
        reg, regfiles = R.command_registry("magic", clone)
        vocab = set()
        for cs in reg.values():
            vocab |= cs
        assert vocab == {"crosshair", "drc"}, vocab
        assert "dbwind/DBWcommands.c" in regfiles, "the dispatch table must be excluded"
        surface, why = R.command_surface("magic", vocab, roots)
        assert set(surface) == {"drc"}, (surface, why)
        assert surface["drc"] == 2, "the surface carries its own evidence count"
        # an empty vocabulary is UNKNOWN, never "we issue nothing"
        empty, why_empty = R.command_surface("magic", set(), roots)
        assert empty is None and why_empty, (empty, why_empty)
    src = (Path(__file__).resolve().parent / "reachability.py").read_text()
    assert re.search(r'^\s*(MAGIC_)?COMMANDS\s*=\s*[\[{(]', src, re.M) is None, \
        "a hand-listed command surface came back"


def _unreachable_stub(tool, sha_full):
    return {"verdict": "unreachable", "commands": ["crosshair"], "surface": ["drc", "extract"],
            "closure_complete": True, "symbols": ["CmdCrosshair"],
            "detail": "the changed symbol(s) CmdCrosshair are reachable only from "
                      "`crosshair`, and we never issue that"}


def test_assess_discloses_the_disagreement_and_does_not_auto_propose():
    """End to end: the model says relevant, the surface says unreachable. The row must
    state BOTH, drop out of clearly-safe, and not silently keep the judge's reason."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            _cache_fixture(tmp, ["low"])
            A._reachability = _unreachable_stub
            # the real reason string from the live 2026-07-28 magic report
            A.classify_commits = lambda tool, role, commits: {
                c["sha"]: {"category": "bugfix", "relevant": True, "risk": "low",
                           "summary": "Prevents crash in headless mode — critical for "
                                      "automated batch DRC/extraction runs",
                           "reproduce": "", "recommend": "adopt"} for c in commits}
            rep = A.assess("magic")
        finally:
            _pop_state_dir()
    assert rep["clearly_safe"] == [], "an unreachable commit was auto-proposed"
    assert rep["unreachable"] == ["cc4da9a05fde"], rep.get("unreachable")
    row = rep["commits"][0]
    assert row["decision"] == "human"
    assert row["relevant"] is True, "the model's verdict must NOT be rewritten"
    assert row["reachability"]["verdict"] == "unreachable"
    # requirement 4: the rendered reason may not claim a relevance the analysis contradicts
    assert row["summary"].startswith("⚠ UNREACHABLE FROM OUR SURFACE")
    assert row["judge_summary"] == ("Prevents crash in headless mode — critical for "
                                    "automated batch DRC/extraction runs"), \
        "the judge's own reason must survive verbatim"
    assert "the judge nonetheless called it relevant" in row["summary"]
    md = A.render_md(rep)
    assert "MODEL / SURFACE DISAGREEMENT on 1 commit(s)" in md
    assert "NEITHER has been resolved away" in md
    assert "⚠ NOT ours" in md, "the reach column must show the disagreement"
    row_line = next(ln for ln in md.splitlines() if ln.startswith("| `cc4da9a05fde`"))
    assert "**human**" in row_line
    # the CONTRADICTING half must survive the summary-column truncation: at 110 chars
    # the row stopped at "reachable only from `" and never named the command, nor said
    # we do not issue it
    assert "we never issue that" in row_line, row_line
    assert "the judge nonetheless called it relevant" in row_line, row_line
    assert "batch DRC/extraction runs" in row_line, \
        "the claim being contradicted was itself truncated away"
    assert row_line.rstrip().endswith('" |'), f"the disclosure cell was cut: {row_line}"


def test_a_reachable_candidate_is_still_auto_safe_end_to_end():
    """The fix must not cost the happy path: a reachable low-risk bugfix still qualifies."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            _cache_fixture(tmp, ["low"])
            A._reachability = lambda tool, sha: {
                "verdict": "reachable", "commands": ["drc", "extract"],
                "surface": ["drc"], "closure_complete": True, "symbols": ["DRCBasicCheck"],
                "detail": "reached from drc, which our emitters issue"}
            rep = A.assess("magic")
        finally:
            _pop_state_dir()
    assert rep["clearly_safe"] == ["cc4da9a05fde"], rep["clearly_safe"]
    assert rep["unreachable"] == []
    md = A.render_md(rep)
    assert "✓ ours" in md
    assert "MODEL / SURFACE DISAGREEMENT" not in md


def test_reach_column_says_not_probed_when_it_did_not_run():
    """Absence of the check must never render as 'checked, reachable' — the same rule
    the conflict / clean-pick columns already obey."""
    rep = _provenance_rep(assessor="a" * 12, assessed_at="2026-07-28T00:00:00Z")
    rep["commits"][0].pop("reachability", None)
    md = A.render_md(rep)
    row = next(ln for ln in md.splitlines() if ln.startswith("| `aaa111`"))
    cells = [x.strip() for x in row.split("|")]
    assert cells[7] == "not-probed", row
    assert cells[5] == "—" and cells[6] == "✓", "the older columns must not have shifted"


def test_the_disagreement_reaches_the_gatekeeper_note_and_the_pr_body():
    """The disclosure has to reach the two places a human actually reads — driven
    through the real builders, not grepped out of their source."""
    rep = _rep(commit_count=2, outstanding=["a", "b"], unreachable=["a"])
    note = _gk().assessment_entry(rep, 1, "v2")["note"]
    assert "1 commit(s) the judge called relevant are UNREACHABLE" in note
    assert "UNREACHABLE from our command surface" in _pn().tally_line("magic", rep)


def test_the_reachability_check_is_part_of_the_assessor_identity():
    """It decides what a row says, so editing it must re-judge cached ranges rather
    than have the change masked by the cache (vibeic/vibeic-eda#4)."""
    assert any(p.name == "reachability.py" for p in A.ASSESSOR_SOURCES), A.ASSESSOR_SOURCES
    import shutil
    real = Path(__file__).resolve().parent / "reachability.py"
    orig = A.ASSESSOR_SOURCES
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "reachability.py"
        shutil.copy(real, copy)
        try:
            A.ASSESSOR_SOURCES = (copy,)
            before = A.assessor_id()
            copy.write_text(copy.read_text() + "\n# tighten the surface filter\n")
            after = A.assessor_id()
        finally:
            A.ASSESSOR_SOURCES = orig
    assert before != after


# ═════════════════════════════════════════════════════════════════════════════
# vibeic/vibeic-eda#6 — a verdict only ONE sample supports must not auto-adopt
#
# temperature=0 removed the variance it could and no more. MEASURED 2026-07-28: the
# identical 105-commit magic range, one assessor, one prompt, cache bypassed, judged
# three times returned useful sets of 2 / 4 / 2 — three different answers to one
# question — while all 315 `risk` gradings were identical. Every commit that moved was
# a borderline one, which is exactly the population `_clearly_safe` decides, and a
# clearly-safe verdict opens a real cherry-pick PR.
#
# The treatment is deliberately NARROW: re-sample only the commits that already passed
# every OTHER clearly-safe condition (1 of 105 on that range), never the range.

def _clearly_safe_BEFORE_SAMPLING(cls, touches_our_files, clean_pick, reach=None):
    """FROZEN copy of the gate as it stood at 1b36787 — after #5 added `reach`, before
    #6 added the confirmation. This is the baseline for the strictness proof below. Do
    NOT 'fix' it to match the live gate: its entire job is to disagree if the live one
    ever loosens."""
    if reach is not None and reach.get("verdict") == "unreachable":
        return False
    return (cls.get("category") == "bugfix"
            and cls.get("risk") == "low"
            and cls.get("relevant") is True
            and cls.get("recommend") == "adopt"
            and not touches_our_files
            and clean_pick is True)


def test_clearly_safe_is_no_looser_after_the_confirmation_requirement():
    """EXHAUSTIVE over the gate's WHOLE input domain, new axis included: every input the
    NEW gate calls auto-adoptable, the pre-#6 gate called auto-adoptable too."""
    import itertools
    cats = ["bugfix", "other", "feature", A.NOT_ASSESSED, None]
    risks = ["low", "medium", "high", A.NOT_ASSESSED, None]
    rels = [True, False, None]
    recs = ["adopt", "skip", "manual", None]
    tri = [True, False, None]
    reaches = [None, {"verdict": "reachable"}, {"verdict": "unreachable"},
               {"verdict": "unknown"}]
    agrees = [None, {}, {"agree": True, "complete": True}, {"agree": False, "complete": True},
              {"agree": True, "complete": False}]
    n = looser = new_admits = old_admits = 0
    for cat, risk, rel, rec, t, cp, rc, ag in itertools.product(
            cats, risks, rels, recs, tri, tri, reaches, agrees):
        cls = {"category": cat, "risk": risk, "relevant": rel, "recommend": rec}
        n += 1
        new = A._clearly_safe(cls, t, cp, rc, ag)
        old = _clearly_safe_BEFORE_SAMPLING(cls, t, cp, rc)
        old_admits += bool(old)
        new_admits += bool(new)
        if new and not old:
            looser += 1
    assert n == 5 * 5 * 3 * 4 * 3 * 3 * 4 * 5 == 54000, n
    assert looser == 0, f"{looser}/{n} inputs became NEWLY auto-adoptable"
    # STRICTLY stricter, and not vacuously so: the gate still admits the confirmed
    # candidate it is supposed to admit. A gate that rejects everything would also
    # score `looser == 0` and would be useless.
    assert new_admits > 0, "the gate admits nothing at all — vacuously strict"
    assert new_admits < old_admits, (new_admits, old_admits)


def test_one_sample_alone_can_never_be_auto_adopted():
    """The issue in one assertion: the pre-#6 gate said yes on a single reading."""
    perfect = {"category": "bugfix", "risk": "low", "relevant": True, "recommend": "adopt"}
    assert _clearly_safe_BEFORE_SAMPLING(perfect, False, True, {"verdict": "reachable"}) is True
    assert A._clearly_safe(perfect, False, True, {"verdict": "reachable"}) is False


# ── llm_judge.confirm: what counts as agreement ──────────────────────────────
def test_confirm_agrees_only_when_every_sample_matches():
    import llm_judge as J
    commits = [{"sha": "sha001", "title": "fix"}]
    first = {"sha001": (True, "low")}
    same = J.confirm(commits, first, lambda cs: {"sha001": (True, "low")}, extra=2)["sha001"]
    assert same.agree is True and same.complete is True
    assert same.readings == ((True, "low"), (True, "low"), (True, "low"))
    assert "3 independent judgements agreed" in same.detail

    # `useful` flips — the field the measurement showed moving
    flip = J.confirm(commits, first, lambda cs: {"sha001": (False, "low")}, extra=1)["sha001"]
    assert flip.agree is False and flip.complete is True
    assert "DISAGREED" in flip.detail
    assert "#1 useful=true, risk=low" in flip.detail and "#2 useful=false" in flip.detail

    # `risk` flips — it did not move in 315 measured gradings, but the gate reads it,
    # and it is the field that oscillated medium<->low before temperature was pinned
    rf = J.confirm(commits, first, lambda cs: {"sha001": (True, "medium")}, extra=1)["sha001"]
    assert rf.agree is False and "risk=medium" in rf.detail


def test_a_sample_that_never_arrived_is_not_agreement():
    """REQUIREMENT 4. A failed re-sample call is not a confirmation — it is a demotion,
    and it is TRANSIENT, which the `complete` flag is what tells the cache."""
    import llm_judge as J
    commits = [{"sha": "sha001", "title": "fix"}]
    first = {"sha001": (True, "low")}
    for sampler in (lambda cs: {},                                   # call returned nothing
                    lambda cs: {"sha001": None},                     # sha omitted / unassessed
                    lambda cs: (_ for _ in ()).throw(RuntimeError("boom")),   # call exploded
                    lambda cs: "not a dict"):                        # nonsense shape
        a = J.confirm(commits, first, sampler, extra=2)["sha001"]
        assert a.agree is False, sampler
        assert a.complete is False, sampler
        assert "never arrived is not agreement" in a.detail
        assert "only 1 of 3" in a.detail
    # and a MISSING first reading is the same failure from the other end
    a = J.confirm(commits, {}, lambda cs: {"sha001": (True, "low")}, extra=1)["sha001"]
    assert a.agree is False and a.complete is False


def test_confirm_disclosure_prints_every_reading_never_a_majority():
    """REQUIREMENT 2. 2-of-3 is not a verdict — it is a disagreement, and both sides
    of it have to be legible."""
    import llm_judge as J
    commits = [{"sha": "sha001", "title": "fix"}]
    seq = [{"sha001": (False, "low")}, {"sha001": (True, "low")}]
    a = J.confirm(commits, {"sha001": (True, "low")}, lambda cs: seq.pop(0), extra=2)["sha001"]
    assert a.agree is False, "a 2-of-3 majority was silently adopted"
    assert a.readings == ((True, "low"), (False, "low"), (True, "low"))
    for want in ("#1 useful=true", "#2 useful=false", "#3 useful=true"):
        assert want in a.detail, a.detail


def test_confirm_is_bounded_and_never_calls_the_api_itself():
    """The sampler is INJECTED, so `judge()` stays the only token-spending call in the
    module, and the sample count is clamped so a bad env var cannot run away."""
    import llm_judge as J
    commits = [{"sha": "sha001", "title": "fix"}]
    for asked, want in ((1, 1), (0, 1), (-5, 1), (99, 8), ("junk", 1), (None, J.SAMPLES - 1)):
        seen = []
        J.confirm(commits, {"sha001": (True, "low")},
                  lambda cs: seen.append(1) or {"sha001": (True, "low")}, extra=asked)
        assert len(seen) == want, (asked, len(seen))
    # CODE, not prose. `inspect.getsource` returns the docstring and every
    # comment, so a line in `confirm`'s own documentation saying it must never
    # call `urlopen` would turn this red — the test would punish the sentence
    # that states the property it enforces, and the fix would be to delete the
    # explanation. Same trap in the other direction as the vibe-ic#551 ordering
    # check, which read six step names out of comments and reported all six as
    # violations.
    import ast
    import inspect
    tree = ast.parse(textwrap.dedent(inspect.getsource(J.confirm)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    called |= {n.func.attr for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for banned in ("urlopen", "_judge_chunk"):
        assert banned not in called, (
            f"`confirm` calls {banned}() — it must stay a pure combinator over "
            f"an INJECTED sampler, or `judge()` is no longer the only "
            f"token-spending call in the module")


def test_the_sample_count_has_a_floor_and_survives_a_garbage_env():
    """GK_JUDGE_SAMPLES may RAISE the bar, never switch the confirmation off; and a
    typo in it must not make the judge unimportable (that degrades every commit)."""
    import importlib
    import llm_judge as J
    try:
        for val, want in (("1", 2), ("0", 2), ("-3", 2), ("", 3), ("three", 3),
                          ("4", 4), ("500", 9)):
            os.environ["GK_JUDGE_SAMPLES"] = val
            importlib.reload(J)
            assert J.SAMPLES == want, (val, J.SAMPLES)
    finally:
        os.environ.pop("GK_JUDGE_SAMPLES", None)
        importlib.reload(J)
    assert J.SAMPLES == 3


def test_the_sample_count_is_part_of_the_assessor_identity():
    """A verdict confirmed once and a verdict confirmed twice are different claims, so
    they must not share a cache slot (vibeic/vibeic-eda#4's rule, applied to #6's knob)."""
    import importlib
    import llm_judge
    base = A.assessor_id()
    try:
        os.environ["GK_JUDGE_SAMPLES"] = "5"
        importlib.reload(llm_judge)
        assert A.assessor_id() != base, "GK_JUDGE_SAMPLES did not move the assessor id"
    finally:
        os.environ.pop("GK_JUDGE_SAMPLES", None)
        importlib.reload(llm_judge)
    assert A.assessor_id() == base
    assert "samples" in A._assessor_knobs()


def test_the_confirmation_routes_through_the_same_classifier_the_first_reading_used():
    """The stub path must reach the round, not bypass it — otherwise every stub-driven
    caller is silently locked out of auto-adopt, and the comparison code goes untested
    on the path most tests take. A stub is deterministic, so it AGREES, honestly."""
    cands = [{"sha": "sha001", "title": "fix drc null deref", "body": ""}]
    verdict = {"category": "bugfix", "relevant": True, "risk": "low",
               "summary": "s", "reproduce": "", "recommend": "adopt"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"sha001": verdict}, f)
        sp = f.name
    try:
        os.environ["GK_ASSESS_STUB"] = sp
        got = A._confirm_candidates("magic", "DRC", cands, {"sha001": verdict})
    finally:
        os.environ.pop("GK_ASSESS_STUB", None)
        os.unlink(sp)
    assert got["sha001"]["agree"] is True and got["sha001"]["complete"] is True
    assert got["sha001"]["readings"] == [[True, "low"]] * 3
    assert A._clearly_safe(verdict, False, True, None, got["sha001"]) is True


def test_the_ai_kill_switch_cannot_confirm_anything():
    """GK_ASSESS_AI=0 yields no verdict, and no verdict is not agreement — the
    deterministic-only mode must never be a back door into the auto-adopt tier."""
    cands = [{"sha": "sha001", "title": "fix", "body": ""}]
    verdict = {"category": "bugfix", "relevant": True, "risk": "low",
               "summary": "s", "reproduce": "", "recommend": "adopt"}
    os.environ["GK_ASSESS_AI"] = "0"
    try:
        got = A._confirm_candidates("magic", "DRC", cands, {"sha001": verdict})
    finally:
        os.environ.pop("GK_ASSESS_AI", None)
    assert got["sha001"]["agree"] is False and got["sha001"]["complete"] is False
    assert A._clearly_safe(verdict, False, True, None, got["sha001"]) is False


def test_an_unimportable_or_exploding_judge_is_not_agreement():
    """Every escape hatch out of the confirmation lands on 'not confirmed'."""
    import llm_judge
    cands = [{"sha": "sha001", "title": "fix", "body": ""}]
    verdict = {"category": "bugfix", "relevant": True, "risk": "low",
               "recommend": "adopt", "summary": "s", "reproduce": ""}
    orig = llm_judge.confirm
    try:
        llm_judge.confirm = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        got = A._confirm_candidates("magic", "DRC", cands, {"sha001": verdict})
        assert got["sha001"]["agree"] is False
        assert "rests on ONE sample" in got["sha001"]["detail"]
        llm_judge.confirm = lambda *a, **k: {}          # returned nothing for the sha
        got = A._confirm_candidates("magic", "DRC", cands, {"sha001": verdict})
        assert got["sha001"]["agree"] is False
        assert "returned nothing for this" in got["sha001"]["detail"]
    finally:
        llm_judge.confirm = orig
    assert A._confirm_candidates("magic", "DRC", [], {}) == {}


# ── end to end through assess(), against the real HTTP layer (stubbed) ───────
def _mixed_reply(shas, useful, risk="low"):
    """A complete, well-formed judge reply marking `useful` the shas in that set."""
    return {"content": [{"type": "text", "text": "{\n" + ",\n".join(
        f'  "{s}": {{"useful": {"true" if s in useful else "false"}, '
        f'"reason": "judged {s}", "risk": "{risk}"}}' for s in shas) + "\n}"}],
        "stop_reason": "end_turn"}


def _sequenced_api(replies, sent):
    """urlopen stub answering request #i from replies[i] (the last entry repeats)."""
    n = {"i": 0}

    def make(shas):
        i = n["i"]
        n["i"] += 1
        return replies[min(i, len(replies) - 1)](shas)
    return _stub_api(make, sent)


def _e2e(tmp, replies, commits=None, touches=False):
    """assess() with gh/git stubbed but classify_commits, llm_judge and the
    confirmation round ALL REAL — only the HTTP layer is faked."""
    import importlib
    import urllib.request as U
    import llm_judge
    os.environ["GK_STATE_DIR"] = str(tmp)
    os.environ.pop("GK_ASSESS_STUB", None)
    importlib.reload(A)
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)
    (tmp / "ledger" / "magic.json").write_text(json.dumps({
        "tool": "magic", "integrated": True, "behind_releases": 1,
        "upstream": "up/magic", "upstream_default_branch": "master",
        "pinned_ref_full": "a" * 40, "base_release": "8.3.674",
        "upstream_latest_release": "8.3.678", "role": "DRC"}))
    cs = commits or [{"sha": f"sha{i:03d}", "sha_full": f"{i:040d}", "title": "t",
                      "body": "", "url": "", "author": "x"} for i in range(5)]
    A.upstream_commits = lambda *a: (cs, ["f.c"])
    A.our_patch_files = lambda *a: {"ours.c"} if touches else set()
    A._commit_files = lambda *a: {"ours.c"} if touches else {"f.c"}
    A.clean_cherrypick = lambda *a: True
    A.already_carried = lambda *a: set()
    A.recorded_decisions = lambda *a: {}
    A._reachability = lambda *a: None          # undetermined → verdict stands (#5's rule)
    sent = []
    orig_tok, orig_open = llm_judge._token, U.urlopen
    try:
        llm_judge._token = lambda: "stub-token"
        U.urlopen = _sequenced_api(replies, sent)
        return A.assess("magic"), sent
    finally:
        llm_judge._token, U.urlopen = orig_tok, orig_open
        os.environ.pop("GK_STATE_DIR", None)
        importlib.reload(A)


def test_agreeing_samples_auto_adopt_and_cost_only_the_candidates():
    """REQUIREMENT 5, the load-bearing one: the extra requests ask about the CANDIDATE
    ONLY. 105 x N per tick is not the fix; N over the handful that already passed
    everything else is."""
    with tempfile.TemporaryDirectory() as d:
        rep, sent = _e2e(Path(d), [lambda shas: _mixed_reply(shas, {"sha002"})])
    assert rep["clearly_safe"] == ["sha002"], rep["clearly_safe"]
    assert rep["unconfirmed"] == []
    assert rep["judge_samples"] == 3
    # 1 first reading over the whole range + (SAMPLES-1) confirmations
    assert len(sent) == 3, [_shas_in(b) for b in sent]
    assert _shas_in(sent[0]) == [f"sha{i:03d}" for i in range(5)], "the range moved"
    assert _shas_in(sent[1]) == ["sha002"], "a confirmation re-judged more than the candidate"
    assert _shas_in(sent[2]) == ["sha002"]
    md = A.render_md(rep)
    assert "✓ 3/3" in md
    assert "JUDGEMENT DID NOT REPRODUCE" not in md


def test_a_verdict_only_one_sample_supports_drops_to_human_with_both_readings():
    """REQUIREMENTS 1 + 2. The FIRST reading says adopt; a confirmation disagrees. The
    commit must not auto-adopt, and the report must print BOTH readings — not the
    2-of-3 majority, which here would still say 'useful'."""
    with tempfile.TemporaryDirectory() as d:
        rep, sent = _e2e(Path(d), [lambda shas: _mixed_reply(shas, {"sha002"}),
                                   lambda shas: _mixed_reply(shas, set()),
                                   lambda shas: _mixed_reply(shas, {"sha002"})])
    assert rep["clearly_safe"] == [], "a majority of samples was silently adopted"
    assert rep["unconfirmed"] == ["sha002"], rep["unconfirmed"]
    row = next(c for c in rep["commits"] if c["sha"] == "sha002")
    assert row["decision"] == "human"
    assert row["sampling_conflict"] is True
    assert row["judge_summary"] == "judged sha002", "the first reading's reason was lost"
    for want in ("JUDGEMENT DID NOT REPRODUCE", "#1 useful=true", "#2 useful=false",
                 "#3 useful=true", "judged sha002"):
        assert want in row["summary"], row["summary"]
    # every reading survives as data too, not only as prose
    assert row["agreement"]["readings"] == [[True, "low"], [False, "low"], [True, "low"]]
    md = A.render_md(rep)
    assert "JUDGEMENT DID NOT REPRODUCE on 1 commit(s)" in md
    assert "none has been averaged into a majority" in md
    assert "⚠ DIVERGED" in md
    # and the disclosure is not truncated away in the summary column
    assert "#2 useful=false" in md, "the contradicting reading was cut off"


def test_a_failed_resample_call_is_not_agreement_and_is_not_cached():
    """REQUIREMENT 4. An outage during the confirmation must demote (fail closed) AND
    stay provisional, so the next tick can re-resolve it instead of the failure being
    frozen in as a permanent verdict."""
    import urllib.error

    def boom(shas):
        raise urllib.error.URLError("connection reset")

    with tempfile.TemporaryDirectory() as d:
        rep, sent = _e2e(Path(d), [lambda shas: _mixed_reply(shas, {"sha002"}), boom])
        assert rep["clearly_safe"] == []
        assert rep["unconfirmed"] == ["sha002"]
        row = next(c for c in rep["commits"] if c["sha"] == "sha002")
        assert "never arrived is not agreement" in row["summary"], row["summary"]
        assert row["agreement"]["complete"] is False
        # re-running must RE-JUDGE, not replay a frozen outage
        rep2, _ = _e2e(Path(d), [lambda shas: _mixed_reply(shas, {"sha002"})])
        assert not rep2.get("cached"), "a transient confirmation failure was cached"
        assert rep2["clearly_safe"] == ["sha002"], "the range never re-resolved"


def test_a_genuine_disagreement_does_cache():
    """The other half: a DISAGREEMENT is a finding, not an outage. Re-judging it every
    tick would reintroduce exactly the daily re-drift the cache exists to stop."""
    replies = [lambda shas: _mixed_reply(shas, {"sha002"}),
               lambda shas: _mixed_reply(shas, set())]
    with tempfile.TemporaryDirectory() as d:
        rep1, _ = _e2e(Path(d), replies)
        assert rep1["unconfirmed"] == ["sha002"]
        rep2, sent2 = _e2e(Path(d), replies)
        assert rep2.get("cached") is True, "a settled disagreement was re-judged"
        assert sent2 == [], "the cache replay still spent API calls"


def test_nothing_is_resampled_when_another_condition_already_failed():
    """REQUIREMENT 5 from the other side: a candidate that overlaps our carried patches
    is already not auto-adoptable, so re-judging it would buy nothing and cost tokens."""
    with tempfile.TemporaryDirectory() as d:
        rep, sent = _e2e(Path(d), [lambda shas: _mixed_reply(shas, {"sha002"})],
                         touches=True)
    assert rep["clearly_safe"] == []
    assert rep["unconfirmed"] == [], "a commit that failed another gate was re-sampled"
    assert len(sent) == 1, f"the confirmation round ran anyway: {[_shas_in(b) for b in sent]}"
    row = next(c for c in rep["commits"] if c["sha"] == "sha002")
    assert row["decision"] == "human" and row["agreement"] is None
    md = A.render_md(rep)
    assert "not-probed" in md, "an un-run confirmation must not render as confirmed"


def test_an_unassessed_range_never_reaches_the_confirmation_round():
    """A commit the judge never classified is not an adopt-candidate, so it costs no
    re-sample either — the not-assessed path and the #6 path must not interact."""
    with tempfile.TemporaryDirectory() as d:
        rep, sent = _e2e(Path(d), [lambda shas: _truncated_payload(shas, keep=3)])
    assert rep["clearly_safe"] == []
    assert rep["unconfirmed"] == []
    assert set(rep["not_assessed"]) == {"sha003", "sha004"}
    assert len(sent) == 1, "an unassessed range spent confirmation calls"


def test_the_confirmation_reaches_the_gatekeeper_note_and_the_pr_body():
    """The disclosure has to reach the two places a human actually reads — the same
    shape #5's reachability disagreement is held to, and driven the same way."""
    rep = _rep(commit_count=2, outstanding=["a", "b"], unconfirmed=["a"])
    entry = _gk().assessment_entry(rep, 1, "v2")
    assert "JUDGEMENT DID NOT REPRODUCE across independent samples" in entry["note"]
    assert entry["assessed"]["unconfirmed"] == 1
    line = _pn().tally_line("magic", rep)
    assert "DID NOT REPRODUCE" in line and "none averaged" in line


def test_the_render_survives_a_report_that_predates_the_confirmation():
    """An archived report has no `unconfirmed` key and no per-row `agreement`. It must
    render, and its rows must say the confirmation DID NOT RUN — never that it passed."""
    rep = _provenance_rep(assessor="a" * 12, assessed_at="2026-07-01T00:00:00Z")
    rep.pop("unconfirmed", None)
    md = A.render_md(rep)
    assert "JUDGEMENT DID NOT REPRODUCE" not in md
    row = next(ln for ln in md.splitlines() if ln.startswith("| `aaa111`"))
    cells = [x.strip() for x in row.split("|")]
    assert cells[8] == "not-probed", row
    assert cells[5] == "—" and cells[6] == "✓" and cells[7] == "not-probed", \
        "the older columns shifted"


# ── one derivation of the headline counts (vibeic/vibeic-eda#7) ──────────────
# The 2026-07-28 magic tick published three different answers to one question:
# "108 upstream commits — how many need a human?". The assessment table said 105,
# the daily report said 105, and the PR body the same tick opened said 108. Re-run
# on the repaired assessment (1 clearly-safe, 2 outstanding) the split widens: 2 / 2
# / 107. Each site derived the number for itself, and two of the three derivations
# were subtraction.

# The real cached verdict for magic 8.3.674 → 8.3.678, assessor b38988077cdc95ed,
# reduced to the fields the documents read. Kept as a FIXTURE because the numbers are
# what regressed: 108 commits of which 2 are carried, 1 has a recorded skip, 1 is
# clearly-safe and 2 need a human — the other 102 are `rec=skip` CI/build commits that
# need no adoption decision and must not be counted as open work.
MAGIC_0728 = {
    "tool": "magic", "status": "assessed", "base_release": "8.3.674", "latest": "8.3.678",
    "commit_count": 108, "our_patch_files": 59,
    "assessor": "b38988077cdc95ed", "assessed_at": "2026-07-27T23:07:21Z",
    "clearly_safe": ["be83d2954d53"],
    "outstanding": ["86fbd2b50f81", "3f1747b1fb91"],
    "carried": ["a22b7508acfe", "cc4da9a05fde"],
    "decided": ["42b346e31887"],
    "not_assessed": [], "unreachable": ["3f1747b1fb91"], "unconfirmed": [],
    "commits": [{"sha": "be83d2954d53", "title": "t", "decision": "auto-safe",
                 "category": "bugfix", "recommend": "adopt"}],
}


def test_every_document_of_one_tick_states_the_same_counts():
    """The regression, on the range that produced it."""
    md = A.render_md(MAGIC_0728)
    entry = _gk().assessment_entry(MAGIC_0728, 4, "8.3.678")
    line = _pn().tally_line("magic", MAGIC_0728)

    want = {"clearly_safe": 1, "carried": 2, "decided": 1, "outstanding": 2}
    assert A.parse_headline("assessment", md) == want, md.splitlines()[2]
    assert A.parse_headline("report", entry["note"]) == want, entry["note"]
    assert A.parse_headline("pr", line) == want, line
    for field, n in want.items():
        assert entry["assessed"][field] == n, entry["assessed"]
    # and the check that would have caught it agrees they agree
    assert A.cross_check(MAGIC_0728, {"assessment": md, "report": entry["note"],
                                      "pr": line}) == []


def test_the_pr_body_no_longer_answers_with_subtraction():
    """`commit_count - clearly_safe` was UNCONDITIONAL in the PR body — it never read
    `outstanding`, not even as a fallback, so it was wrong even on a complete report."""
    line = _pn().tally_line("magic", MAGIC_0728)
    assert "107" not in line, line
    assert "2 need human review" in line, line


def test_the_structured_value_is_reached_before_any_arithmetic():
    """A `cc - safe`-shaped answer means the structured field was missed. The rows are
    the structured field when the summary list is gone, so arithmetic must not run."""
    rows = [{"decision": "auto-safe"}, {"decision": "carried"},
            {"decision": "recorded:skip"}, {"decision": "human", "recommend": "manual"},
            {"decision": "human", "recommend": "skip"}]
    rep = {"tool": "t", "status": "assessed", "commit_count": 5, "commits": rows}
    n = A.summary_counts(rep)
    # outstanding is 2, not 1: `recorded:skip` is a settled decision and is counted
    # under `decided`, but `human` + `recommend: "skip"` is only the assessor's
    # suggestion on a row nobody has decided.
    assert (n["clearly_safe"], n["carried"], n["decided"], n["outstanding"]) == (1, 1, 1, 2)
    assert n["derived"] == [], "the rows were available and arithmetic ran anyway"
    # a corrupt (non-list) summary field must fall through to the rows, not blow up
    n2 = A.summary_counts({**rep, "outstanding": 4})
    assert n2["outstanding"] == 2, n2


def test_an_unknown_count_still_reads_as_needing_review():
    """The fail-safe DIRECTION of the old fallback is kept: with nothing to read, no
    commit may be claimed safe or settled, so everything unaccounted-for is open."""
    n = A.summary_counts({"tool": "t", "status": "assessed", "commit_count": 9})
    assert n["clearly_safe"] == 0 and n["carried"] == 0 and n["decided"] == 0
    assert n["outstanding"] == 9, n
    assert set(n["derived"]) >= {"clearly_safe", "carried", "decided", "outstanding"}


def test_cross_check_names_the_field_and_both_readings():
    """It must say WHICH number disagrees and what each document claims — a bare
    'documents disagree' sends the reader back to diffing two files by hand."""
    md = A.render_md(MAGIC_0728)
    stale = A.render_md({**MAGIC_0728, "clearly_safe": [], "outstanding": ["x"] * 105})
    bad = A.cross_check(MAGIC_0728, {"assessment": stale})
    assert any("outstanding" in b and "105" in b and "2" in b for b in bad), bad
    assert any("clearly_safe" in b for b in bad), bad
    assert A.cross_check(MAGIC_0728, {"assessment": md}) == []


def test_a_document_that_states_no_counts_is_skipped_not_failed():
    """An assessment error, or a clean/not-layered stub, has nothing to disagree with."""
    err = {"tool": "t", "error": "compare failed"}
    assert A.cross_check(err, {"assessment": A.render_md(err)}) == []
    clean = {"tool": "t", "status": "clean", "commits": []}
    assert A.cross_check(clean, {"assessment": A.render_md(clean)}) == []
    # ...and the RESOLVED phrasing of the daily report, which states no clearly-safe or
    # outstanding number because both are zero, is parsed as the zeros it asserts.
    resolved = _rep(commit_count=3, carried=["a", "b"], decided=["c"])
    note = _gk().assessment_entry(resolved, 2, "v2")["note"]
    assert A.parse_headline("report", note) == {"clearly_safe": 0, "carried": 2,
                                                "decided": 1, "outstanding": 0}
    assert A.cross_check(resolved, {"report": note,
                                    "assessment": A.render_md(resolved)}) == []


def test_the_provenance_stamp_tells_two_vintages_of_one_date_apart():
    """The 2026-07-28 pair. Two assessments of one date, rendered under one filename,
    with nothing on either document saying which judgement it described."""
    md = A.render_md(MAGIC_0728)
    assert A.parse_provenance(md) == {"assessor": "b38988077cdc95ed",
                                      "assessed_at": "2026-07-27T23:07:21Z"}
    entry = _gk().assessment_entry(MAGIC_0728, 4, "8.3.678")
    assert entry["assessed"]["assessor"] == "b38988077cdc95ed"
    assert entry["assessed"]["assessed_at"] == "2026-07-27T23:07:21Z"
    # a report predating provenance pinning states neither, and must not be compared
    assert A.parse_provenance(A.render_md({k: v for k, v in MAGIC_0728.items()
                                           if k not in ("assessor", "assessed_at")})) == {}


def _pin_fleet(gk, where: Path, tools, extra: dict | None = None) -> Path:
    """Point the configuration gate (vibeic/vibeic-eda#10) at a throwaway checkout whose
    COMMITTED fleet list names exactly `tools`, and return that checkout.

    The gate is exercised for real rather than stubbed — the tick tests below run through
    the same `git show HEAD:…` comparison production runs through. What the fixture
    supplies is a repository to compare against: without one the tests would be asserting
    on the state of whichever checkout the suite happens to run in, which is both flaky
    and, once this suite is run from a source tarball, meaningless.

    `extra` overwrites the file AFTER the commit — that is how a test injects the
    hand-edit this gate exists to catch. Pass a dict for a different configuration, or a
    string for the same configuration in different bytes.
    """
    import subprocess
    where.mkdir(parents=True, exist_ok=True)
    fleet = {"org": "vibeic", "forks": [{"tool": t, "role": "test",
                                         "upstream": f"them/{t}"} for t in tools]}
    (where / "FORKS.json").write_text(json.dumps(fleet, indent=2) + "\n")
    (where / "ENHANCEMENTS.json").write_text("{}\n")
    run = lambda *a: subprocess.run(("git", "-C", str(where)) + a,  # noqa: E731
                                    capture_output=True, check=True)
    if not (where / ".git").exists():
        run("init", "-q")
    run("add", "FORKS.json", "ENHANCEMENTS.json")
    if subprocess.run(("git", "-C", str(where), "diff", "--cached", "--quiet"),
                      capture_output=True).returncode:
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fleet")
    if extra is not None:
        (where / "FORKS.json").write_text(
            extra if isinstance(extra, str) else json.dumps(extra, indent=2) + "\n")
    gk.fleet_config.HERE = where
    return where


def _tick_fixture(state: Path, rep, render=None, ledgers=None, on_cross_check=None,
                  fleet_extra=None):
    """Run a REAL tick against a prepared state dir — no network, no PR, no page.

    `disc.main` (which re-seeds the ledgers over the GitHub API) and `assess` (which
    spends the judge) are the only things stubbed; everything from the summary branch
    to the files on disk is the production path — including the vibeic/vibeic-eda#10
    configuration gate, which `_pin_fleet` gives a real committed fleet list to check
    against.
    """
    (state / "ledger").mkdir(parents=True, exist_ok=True)
    leds = ledgers or {"magic": {
        "tool": "magic", "integrated": True, "behind_releases": 4,
        "upstream": "RTimothyEdwards/magic", "image_version": "0.2.30",
        "base_release": "8.3.674", "upstream_latest_release": "8.3.678",
        "pinned_ref_full": "19185c197fba" + "0" * 28}}
    for name, led in leds.items():
        (state / "ledger" / f"{name}.json").write_text(json.dumps(led))
    os.environ["GK_STATE_DIR"] = str(state)
    merge_pr = os.environ.pop("GK_MERGE_PR", None)   # never open a real cherry-pick PR
    gk = _gk()
    was = gk.fleet_config.HERE
    try:
        _pin_fleet(gk, state / "_src", sorted(leds), extra=fleet_extra)
        gk.disc = type("D", (), {"main": staticmethod(lambda: None)})()
        gk.pr_notify = None
        gk.build_page = type("B", (), {"DEFAULT_OUT": None,
                                       "build": staticmethod(lambda *a: None)})()

        class _Shim:
            def __getattr__(self, k):
                return getattr(A, k)

            def assess(self, tool):
                return rep

            if render is not None:
                def render_md(self, r):
                    return render(r)

            if on_cross_check is not None:
                # records the documents the gate was HANDED, which is the only way to
                # tell "the row says X" from "the row the gate read says X"
                def cross_check(self, r, documents):
                    on_cross_check(dict(documents))
                    return A.cross_check(r, documents)
        gk.assess_release = _Shim()
        return gk, gk.tick()
    finally:
        gk.fleet_config.HERE = was      # the module object is shared across _load calls
        os.environ.pop("GK_STATE_DIR", None)
        if merge_pr is not None:
            os.environ["GK_MERGE_PR"] = merge_pr


def test_a_tick_whose_documents_disagree_publishes_neither():
    """The gate. A renderer that drifts — or an assessment regenerated from another
    vintage — must stop the tick, not produce a report beside a table that contradicts
    it. Injected by rendering the STALE 05:32 verdict (0 clearly-safe, 105 open) while
    the report summarises the repaired one (1, 2): the 2026-07-28 pair exactly."""
    stale = {**MAGIC_0728, "clearly_safe": [], "outstanding": ["x"] * 105}
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        try:
            _tick_fixture(state, MAGIC_0728, render=lambda r: A.render_md(stale))
        except Exception as e:            # gatekeeper.CountsDisagree
            assert type(e).__name__ == "CountsDisagree", repr(e)
            assert "nothing published" in str(e)
        else:
            raise AssertionError("the tick published two contradicting documents")
        assert list((state / "reports").glob("*.md")) == [], "a report was published"
        assert list((state / "reports").glob("*.json")) == [], "a report was published"
        assert not (state / "reports" / "assessments").exists(), \
            "an assessment was published beside a report that contradicts it"
        led = json.loads((state / "ledger" / "magic.json").read_text())
        assert not led.get("sync_log"), "the ledger recorded a tick that never published"


def test_a_tick_whose_documents_agree_publishes_both_and_verifies_on_disk():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        gk, summary = _tick_fixture(state, MAGIC_0728)
        date = summary["date"]
        report = json.loads((state / "reports" / f"{date}.json").read_text())
        got = next(r for r in report["results"] if r["tool"] == "magic")
        assert got["assessed"]["clearly_safe"] == 1 and got["assessed"]["outstanding"] == 2
        md = (state / "reports" / "assessments" / f"{date}-magic.md").read_text()
        assert A.parse_headline("assessment", md) == {"clearly_safe": 1, "carried": 2,
                                                      "decided": 1, "outstanding": 2}
        # the round trip the tick runs on itself
        os.environ["GK_STATE_DIR"] = str(state)
        try:
            gk2 = _gk()
            assert gk2.verify_documents(date) == []
            # ...and it CATCHES a later re-render that the report was never regenerated
            # for — the 2026-07-28 failure, reproduced on the published files.
            stale = {**MAGIC_0728, "clearly_safe": [], "outstanding": ["x"] * 105,
                     "assessor": "0" * 16, "assessed_at": "2026-07-27T21:32:00Z"}
            (state / "reports" / "assessments" / f"{date}-magic.md").write_text(
                A.render_md(stale))
            bad = gk2.verify_documents(date)
            assert any("outstanding" in b for b in bad), bad
            assert any("two vintages of one date" in b for b in bad), bad
        finally:
            os.environ.pop("GK_STATE_DIR", None)


# ── vibeic/vibeic-eda#8 — a fork that ships INSIDE another fork's pin ────────────────
#
# The detector modelled one ARG per tool, so a fork vendored as a submodule of a pinned
# fork had no ARG of its own and read as `integrated=False` → "forked but not pinned into
# the image (uses upstream directly) — nothing to sync", about a tool whose enhancements
# the image actually runs. NOT_LAYERED had exactly one member and it was that error.
#
# Every fixture below is named for nothing we ship. The rule has to hold for any host and
# any vendored fork, and a rule proven only on the instance that exposed it is a special
# case wearing a general name — so `Alpha`/`Zeta`/`vendor/zeta` is the test, and the
# production pair is only the thing that made us look.

_ALPHA_DOCKERFILE = """\
FROM base AS alpha-builder
ARG ALPHA_REF=aaaa1111aaaa1111  # pinned; branch vibeic/alpha-line
RUN git clone https://github.com/vibeic/Alpha.git /src \\
 && cd /src && git checkout ${ALPHA_REF} \\
 && git submodule update --init --recursive --depth 1 \\
 && make install

FROM base AS beta-builder
ARG BETA_REF=bbbb2222bbbb2222  # pinned; branch vibeic/beta-line
RUN git clone https://github.com/vibeic/Beta.git /beta \\
 && cd /beta && git checkout ${BETA_REF} \\
 && make install
"""

# Alpha vendors ONE fork of ours and one repository that is not ours. The foreign one is
# written in git's RELATIVE form, which is not a hypothetical: the live OpenROAD
# .gitmodules pins abc as `../../The-OpenROAD-Project/abc.git`, and a reader that treated
# the `..` segments as literal path would have claimed it as a vibeic fork.
_ALPHA_GITMODULES = """\
[submodule "vendor/zeta"]
\tpath = vendor/zeta
\turl = https://github.com/vibeic/Zeta.git
[submodule "third-party/ext"]
\tpath = third-party/ext
\turl = ../../SomeoneElse/ext.git
"""

# Beta declares the SAME vendored fork, but its build never fetches submodules.
_BETA_GITMODULES = """\
[submodule "vendor/omega"]
\tpath = vendor/omega
\turl = https://github.com/vibeic/Omega.git
"""


def _fake_gh(routes):
    """A `gh` that serves a fixed route table — no network, no token."""
    def gh(path):
        if path in routes:
            return routes[path]
        return {"_err": f"unrouted: {path}"}
    return gh


def _b64(text):
    import base64
    return {"content": base64.b64encode(text.encode()).decode()}


def _disc_with_routes(routes):
    d = _load("discover_forks")
    d.gh = _fake_gh(routes)
    return d


_ALPHA_ROUTES = {
    "repos/vibeic/Alpha/contents/.gitmodules?ref=aaaa1111aaaa1111": _b64(_ALPHA_GITMODULES),
    "repos/vibeic/Beta/contents/.gitmodules?ref=bbbb2222bbbb2222": _b64(_BETA_GITMODULES),
    "repos/vibeic/Alpha/contents/vendor/zeta?ref=aaaa1111aaaa1111": {
        "type": "submodule", "sha": "zzzz9999zzzz9999"},
    "repos/vibeic/Zeta/commits/zzzz9999zzzz9999/branches-where-head": [
        {"name": "vibeic/zeta-work"}],
    # Zeta itself declares nothing, so the --recursive descent terminates
    "repos/vibeic/Zeta/contents/.gitmodules?ref=zzzz9999zzzz9999": {"_err": "404"},
    # Omega is fully resolvable — its gitlink and its branch are both here. The ONLY
    # thing standing between it and the ledger is that Beta's build never fetches
    # submodules, so a test that Omega is absent fails the moment that check is dropped
    # instead of passing because the fixture ran out of data.
    "repos/vibeic/Beta/contents/vendor/omega?ref=bbbb2222bbbb2222": {
        "type": "submodule", "sha": "8888"},
    "repos/vibeic/Omega/commits/8888/branches-where-head": [{"name": "vibeic/omega"}],
    "repos/vibeic/Omega/contents/.gitmodules?ref=8888": {"_err": "404"},
    # ...and so is the foreign one, for the same reason: the only thing that keeps
    # SomeoneElse/ext out of our ledger must be that it is not ours.
    "repos/vibeic/Alpha/contents/third-party/ext?ref=aaaa1111aaaa1111": {
        "type": "submodule", "sha": "6666"},
    "repos/SomeoneElse/ext/commits/6666/branches-where-head": [{"name": "main"}],
    "repos/SomeoneElse/ext/contents/.gitmodules?ref=6666": {"_err": "404"},
}


def test_a_fork_vendored_inside_another_pin_reaches_the_image():
    d = _disc_with_routes(_ALPHA_ROUTES)
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(_ALPHA_DOCKERFILE))
    assert "zeta" in pins, "a fork shipped inside another fork's pin was invisible"
    z = pins["zeta"]
    # the gitlink, not the ARG value: the vendored fork ships at the commit the HOST's
    # tree records, which is the only thing that says which version is in the image
    assert z["ref"] == "zzzz9999zzzz9999"
    assert (z["vendored_in"], z["vendored_path"]) == ("Alpha", "vendor/zeta")
    assert z["arg"] == "ALPHA_REF" and z["host_ref"] == "aaaa1111aaaa1111"
    # the branch is recovered from the fork itself, so the merge-PR path has a base
    assert z["branch"] == "vibeic/zeta-work"


def test_the_vendoring_rule_holds_for_a_host_and_fork_it_was_not_written_for():
    """Tool-agnosticism, as behaviour rather than as a claim about the source.

    Nothing in the fixture shares a name, an ARG, a path or an upstream with the pair
    that exposed the defect; the same code finds it. A rule that only fires on the
    instance it was written for is the defect one level up.
    """
    df = _ALPHA_DOCKERFILE.replace("Alpha", "Carrier").replace("ALPHA_REF", "CARRIER_REF")
    routes = {
        "repos/vibeic/Carrier/contents/.gitmodules?ref=aaaa1111aaaa1111": _b64(
            '[submodule "libs/Nested"]\n\turl = git@github.com:vibeic/Nested.git\n'),
        "repos/vibeic/Carrier/contents/libs/Nested?ref=aaaa1111aaaa1111": {
            "type": "submodule", "sha": "7777"},
        "repos/vibeic/Nested/commits/7777/branches-where-head": [{"name": "vibeic/nested"}],
        "repos/vibeic/Nested/contents/.gitmodules?ref=7777": {"_err": "404"},
    }
    d = _disc_with_routes(routes)
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(df))
    assert "nested" in pins, pins
    # the section NAME is the path when no `path =` is given — which is exactly how the
    # live host declares its vendored fork, and an ssh-form url is still our repository
    assert pins["nested"]["vendored_path"] == "libs/Nested"
    assert pins["nested"]["vendored_in"] == "Carrier"
    assert pins["nested"]["arg"] == "CARRIER_REF"


def test_a_declared_submodule_the_build_never_fetches_is_not_in_the_image():
    """`integrated` means REACHES THE IMAGE, and a .gitmodules entry alone does not.

    Beta declares the same kind of relationship Alpha does and its clone step never runs
    `git submodule update --init`, so nothing of Omega is fetched and nothing of Omega is
    built. Reading the declaration as shipment would replace one wrong answer with its
    mirror image.
    """
    d = _disc_with_routes(_ALPHA_ROUTES)
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(_ALPHA_DOCKERFILE))
    assert "omega" not in pins, "a submodule the build never fetches was called shipped"
    assert pins["beta"]["submodules"] is False
    assert pins["alpha"]["submodules"] is True


def test_a_forks_third_party_submodules_are_not_forks_of_ours():
    d = _disc_with_routes(_ALPHA_ROUTES)
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(_ALPHA_DOCKERFILE))
    assert "ext" not in pins, "somebody else's submodule was tracked as our fork"
    # the resolution itself, including git's relative form against the HOST repository
    assert d.submodule_repo("../../SomeoneElse/ext.git", "vibeic/Alpha") == "SomeoneElse/ext"
    assert d.submodule_repo("../sibling.git", "vibeic/Alpha") == "vibeic/sibling"
    assert d.submodule_repo("https://github.com/vibeic/Zeta.git", "vibeic/Alpha") == "vibeic/Zeta"
    assert d.submodule_repo("git@github.com:vibeic/Zeta.git", "vibeic/Alpha") == "vibeic/Zeta"
    assert d.submodule_repo("https://gitlab.com/vibeic/Zeta.git", "vibeic/Alpha") is None
    assert d.submodule_repo("", "vibeic/Alpha") is None


def test_an_arg_pin_is_never_overwritten_by_a_vendored_one():
    """A tool with its own ARG ships by that ARG. If a host also vendors it, the direct
    pin is the more specific statement of how it reaches the image — and the one whose
    bump actually changes what is built."""
    df = _ALPHA_DOCKERFILE + (
        "\nARG ZETA_REF=cccc3333cccc3333  # pinned; branch vibeic/zeta-direct\n"
        "RUN git clone https://github.com/vibeic/Zeta.git /z && cd /z "
        "&& git checkout ${ZETA_REF}\n")
    d = _disc_with_routes(_ALPHA_ROUTES)
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(df))
    assert pins["zeta"]["ref"] == "cccc3333cccc3333"
    assert pins["zeta"]["arg"] == "ZETA_REF"
    assert "vendored_in" not in pins["zeta"]


def test_the_ledger_records_how_an_indirectly_pinned_fork_reaches_the_image():
    """`integrated` is not enough on its own: the row also has to say by WHICH pin, or an
    operator reads the ordinary case (its own ARG) into a tool that has none."""
    d = _disc_with_routes({
        **_ALPHA_ROUTES,
        "repos/vibeic/Zeta": {"parent": {"default_branch": "main"},
                              "created_at": "2020-01-01T00:00:00Z", "default_branch": "main"},
        "repos/vibeic/Zeta/compare/them:main...zzzz9999zzzz9999": {
            "ahead_by": 7, "behind_by": 57, "commits": [],
            "merge_base_commit": {"sha": "m" * 40,
                                  "commit": {"message": "merge base",
                                             "author": {"date": "2026-06-30T00:00:00Z"}}}},
        "repos/them/Zeta/releases?per_page=30": [{"tag_name": "v2.2.0",
                                                  "published_at": "2020-09-14T00:00:00Z"}],
        "repos/vibeic/Zeta/compare/them:v2.2.0...zzzz9999zzzz9999": {
            "ahead_by": 2200, "behind_by": 0},
    })
    pins = d.expand_vendored_pins(d.parse_dockerfile_pins(_ALPHA_DOCKERFILE))
    led = d.discover_one({"tool": "Zeta", "upstream": "them/Zeta", "role": "timing"},
                         pins, "0.2.30")
    assert led["integrated"] is True
    assert led["pinned_ref"] == "zzzz9999zzzz"
    assert led["dockerfile_arg"] == "ALPHA_REF"
    assert led["vendored_in"] == "Alpha" and led["vendored_path"] == "vendor/zeta"
    assert led["pinned_via"] == "ALPHA_REF → vibeic/Alpha vendor/zeta"
    # the patches we carry are now measured against the ref we SHIP, not against a branch
    # of the fork the image never builds
    assert led["ahead"] == 7 and led["behind_commits"] == 57


def test_not_layered_still_holds_a_fork_the_image_never_fetches():
    """The category is kept — #8 was a membership defect, and deleting the category to
    fix membership would lose the honest row too.

    What changed (vibeic-eda#32): the note no longer asserts the image does not
    contain the tool. `integrated = bool(ref)` means the pin resolver found no
    `ARG <TOOL>_REF`, and five of the six tools in this state turned out to BE in
    the image — four from the base image, one we stage ourselves, none of those
    routes modelled. So the row states what is known (no pin, therefore no range
    assessed) and explicitly denies the stronger claim it used to make."""
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        _, summary = _tick_fixture(state, None, ledgers={"unshipped": {
            "tool": "unshipped", "integrated": False, "behind_releases": 0,
            "upstream": "them/unshipped", "image_version": "0.2.30",
            "behind_commits": 12}})
        row = next(r for r in summary["results"] if r["tool"] == "unshipped")
        assert row["verdict"] == "NOT_LAYERED"
        assert row["note"] == ("no `ARG <TOOL>_REF` pin found, so its delivery "
                               "route is unmodelled and no upstream range is "
                               "assessed — this does NOT establish that the tool "
                               "is absent from the image")
        # The load-bearing half: the row must not claim absence. A note that says
        # "uses upstream directly" reads as a verified fact about the image, and
        # nothing verified it.
        assert "absent from the image" in row["note"] and "NOT establish" in row["note"]
        assert summary["counts"]["NOT_LAYERED"] == 1


def test_the_report_row_states_the_pin_indirection():
    """"pinned via `ALPHA_REF` (`vendor/zeta`)" and "pinned via `ZETA_REF`" are different
    instructions to whoever has to rebuild: the vendored copy moves when the HOST is
    rebuilt. A row that says nothing is read as the ordinary case."""
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        _, summary = _tick_fixture(state, None, ledgers={"Zeta": {
            "tool": "Zeta", "integrated": True, "behind_releases": 0,
            "upstream": "them/Zeta", "image_version": "0.2.30", "base_release": "v2.2.0",
            "upstream_latest_release": "v2.2.0", "dockerfile_arg": "ALPHA_REF",
            "vendored_in": "Alpha", "vendored_path": "vendor/zeta",
            "upstream_default_branch": "main", "behind_commits": 0}})
        row = next(r for r in summary["results"] if r["tool"] == "Zeta")
        assert row["verdict"] == "CLEAN"
        assert "pinned via `ALPHA_REF` (`vendor/zeta` in vibeic/Alpha)" in row["note"]
        assert "rebuilding Alpha" in row["note"]
        # and a DIRECTLY pinned fork says nothing extra — the clause marks the exception
        assert _gk().pin_provenance({"dockerfile_arg": "BETA_REF"}) == ""


def test_a_clean_row_discloses_the_upstream_commits_it_never_assessed():
    """CLEAN answers the RELEASE question and used to stop there.

    #8 reported a 48-commit gap that "has never been triaged". Reclassifying the fork does
    not by itself triage it — upstream had cut no new release, so the release-tracking
    verdict is genuinely CLEAN — and leaving the row at "on the latest upstream release"
    is what makes an unreviewed gap read as nothing-to-do. The distance is measured on
    every tick; it is now also stated.
    """
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        _, summary = _tick_fixture(state, None, ledgers={"Zeta": {
            "tool": "Zeta", "integrated": True, "behind_releases": 0,
            "upstream": "them/Zeta", "image_version": "0.2.30", "base_release": "v2.2.0",
            "upstream_latest_release": "v2.2.0", "upstream_default_branch": "master",
            "behind_commits": 57}})
        note = next(r for r in summary["results"] if r["tool"] == "Zeta")["note"]
        assert "on the latest upstream release (v2.2.0)" in note
        assert "57 upstream commit(s) on master are not in our pinned ref" in note
        assert "release-tracking does not assess them" in note
        # a fork that is genuinely level says nothing extra
        assert _gk().unassessed_drift({"behind_commits": 0}) == ""


def test_the_added_clause_is_inside_the_document_the_cross_check_reads():
    """#7's gate parses the numbers back out of the row it is about to publish. A clause
    appended AFTER that check would be published unchecked — and the check would be
    validating a document nobody reads. The assessed row carries both."""
    seen = []
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        _, summary = _tick_fixture(state, MAGIC_0728, on_cross_check=seen.append, ledgers={"magic": {
            "tool": "magic", "integrated": True, "behind_releases": 4,
            "upstream": "RTimothyEdwards/magic", "image_version": "0.2.30",
            "base_release": "8.3.674", "upstream_latest_release": "8.3.678",
            "pinned_ref_full": "19185c197fba" + "0" * 28,
            "dockerfile_arg": "HOST_REF", "vendored_in": "Host",
            "vendored_path": "src/vendored"}})
        row = next(r for r in summary["results"] if r["tool"] == "magic")
        assert "pinned via `HOST_REF` (`src/vendored` in vibeic/Host)" in row["note"]
        # the ordering invariant, observed rather than assumed: what the gate was handed
        # is byte-identical to what was published. A clause appended after the check is
        # published unchecked, and the check is then reading a document nobody sees.
        assert len(seen) == 1, seen
        assert seen[0]["report"] == row["note"]
        # the counts still parse out of the row the gate checked — this is the published
        # text, clause and all
        assert A.parse_headline("report", row["note"]) == {
            "clearly_safe": 1, "carried": 2, "decided": 1, "outstanding": 2}
        assert row["assessed"]["outstanding"] == 2
        date = summary["date"]
        os.environ["GK_STATE_DIR"] = str(state)
        try:
            assert _gk().verify_documents(date) == []
        finally:
            os.environ.pop("GK_STATE_DIR", None)


def test_a_vendored_candidate_is_not_handed_to_the_arg_bumping_harness():
    """The legacy harness rebases a branch and bumps `<TOOL>_REF`. A vendored fork's ARG
    belongs to its HOST, so bumping it to this tool's release sha would repoint the host
    at the wrong repository. Such a candidate belongs on the selective-merge path."""
    gk = _gk()
    seen = {}
    def _spy(cfg, cands):
        seen["cands"] = [c["tool"] for c in cands]
        return {}

    gk._run_harness = _spy
    was = gk.fleet_config.HERE
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        os.environ["GK_STATE_DIR"] = str(state)
        os.environ["GK_RUN_HARNESS"] = "1"
        try:
            _pin_fleet(gk, state / "_src", ["Zeta", "direct"])
            gk.STATE = Path(state)
            gk.LEDGER = Path(state) / "ledger"
            gk.REPORTS = Path(state) / "reports"
            gk.LEDGER.mkdir(parents=True)
            for name, led in {
                "direct": {"tool": "direct", "integrated": True, "behind_releases": 1,
                           "upstream": "them/direct", "upstream_latest_release": "v2"},
                "Zeta": {"tool": "Zeta", "integrated": True, "behind_releases": 1,
                         "upstream": "them/Zeta", "upstream_latest_release": "v2",
                         "dockerfile_arg": "ALPHA_REF", "vendored_in": "Alpha",
                         "vendored_path": "vendor/zeta"}}.items():
                (gk.LEDGER / f"{name}.json").write_text(json.dumps(led))
            gk.disc = type("D", (), {"main": staticmethod(lambda: None)})()
            gk.pr_notify = None

            class _Shim:
                def __getattr__(self, k):
                    return getattr(A, k)

                def assess(self, tool):
                    return _rep(tool=tool, base_release="v1", latest="v2")

            gk.assess_release = _Shim()
            gk.build_page = type("B", (), {"DEFAULT_OUT": None,
                                           "build": staticmethod(lambda *a: None)})()
            gk._image_build_cfg = lambda: {"cmd": "true"}
            gk.tick()
        finally:
            gk.fleet_config.HERE = was
            os.environ.pop("GK_STATE_DIR", None)
            os.environ.pop("GK_RUN_HARNESS", None)
    assert seen.get("cands") == ["direct"], seen


# ── vibeic/vibeic-eda#9 — the guard's coverage is ASSERTED, not inferred ─────────────
#
# #7's `cross_check` reads the four counts back out of rendered text, so its reach is
# whatever `_HEADLINE_RE` still matches. Three separate sites render those counts —
# `assess_release.render_md`, `gatekeeper.assessment_entry`, `pr_notify.tally_line` —
# and not one of them imports the parser, so a reworded headline used to cost nothing:
# `parse_headline` returned None, `cross_check` read None as "this document states no
# counts" and skipped it. Measured on the line that actually shipped in
# vibeic/vibe-ic#508 — "108 upstream commit(s) 8.3.674 → 8.3.678 — 0 clearly-safe, 108
# need review", against an assessment saying 1 and 2 — `parse_headline('pr', …)` was
# None and `cross_check` returned []. The document with the wrongest number in it was
# the one the guard declined to examine.
#
# Two halves below. `states_counts` moves the skip decision off the regex and onto the
# STRUCTURE of the report, so "states none" and "unreadable" stop being the same value;
# and `_RENDERS` is the coverage table — every render site, over every branch its text
# can take for an assessed report, has to hand back the numbers `summary_counts` put in.
# Reword one without teaching the parser and that goes red on the render, rather than
# months later on a published contradiction nothing was checking.

# The exact PR-body phrasing that shipped on 2026-07-28, before #7 gave `tally_line` its
# one wording. Kept verbatim: the point of the fixture is that it is not a strawman.
_SHIPPED_0728_PR_LINE = ("- **magic**: 108 upstream commit(s) 8.3.674 → 8.3.678 — "
                         "0 clearly-safe, 108 need review")


def test_a_document_the_guard_cannot_read_is_a_failure_not_a_skip():
    """The reproduction. An assessed report HAS four numbers, so a document of it that
    states none this program can read back is a render that drifted out of the parser's
    reach — and the fail-safe reading of "I could not check this" is not "it agrees"."""
    assert A.parse_headline("pr", _SHIPPED_0728_PR_LINE) is None, \
        "the fixture is supposed to be a phrasing the parser does not know"
    bad = A.cross_check(MAGIC_0728, {"pr": _SHIPPED_0728_PR_LINE})
    assert bad, "the guard skipped the document with the wrongest number in it"
    assert len(bad) == 1, bad
    assert "pr" in bad[0] and "magic" in bad[0], bad
    assert "outstanding=2" in bad[0], "it must state what the assessment says it should"
    assert "parse_headline" in bad[0], "and name what to repair"
    # ...including the degenerate case: a render that RAISED leaves the caller holding
    # "" (`gatekeeper.tick` passes `rendered.get(tool) or ""`), which is unreadable for
    # the same reason and was skipped for the same reason.
    assert A.cross_check(MAGIC_0728, {"assessment": ""}), "an empty document was skipped"
    # A document that is readable and AGREES is still clean — the failure is unreadable,
    # not unfamiliar.
    assert A.cross_check(MAGIC_0728, {"pr": _pn().tally_line("magic", MAGIC_0728)}) == []


def test_the_skip_is_decided_from_the_report_not_from_the_parse():
    """The other half, and the reason this is not "fail every unparseable document": a
    clean / not-layered fork and an errored entry render a stub with no numbers in it BY
    DESIGN, and that is knowable from `rep` without reading a character of the render. So
    they stay skips, and the common case — most forks, most days — still publishes."""
    stubs = [{"tool": "t", "error": "compare failed"},
             {"tool": "t", "status": "clean", "commits": []},
             {"tool": "t", "status": "not_layered", "commits": []}]
    for rep in stubs:
        assert not A.states_counts(rep), rep
        md = A.render_md(rep)
        assert A.parse_headline("assessment", md) is None, md
        assert A.cross_check(rep, {"assessment": md}) == [], rep
        # and the skip does not depend on WHICH document it is handed: a stub report has
        # nothing to disagree with, whatever text arrives with it
        assert A.cross_check(rep, {"pr": _SHIPPED_0728_PR_LINE,
                                   "report": "", "assessment": md}) == [], rep
    assert A.states_counts(MAGIC_0728), "an assessed range states its counts"
    assert A.states_counts({"tool": "t", "commits": []}), \
        "a report with no `status` key at all is the assess() shape, not a stub"


# Every site that renders the headline counts, keyed by the phrasing `parse_headline`
# selects on. All three are pure functions of one report, which is what makes asserting
# the guard's coverage cost nothing.
_RENDERS = {
    "assessment": lambda rep: A.render_md(rep),
    "report": lambda rep: _gk().assessment_entry(rep, 1, rep.get("latest"))["note"],
    "pr": lambda rep: _pn().tally_line(rep.get("tool", "t"), rep),
}

# Report shapes that reach every branch the three renders can take for an ASSESSED range.
# A coverage table proven on one report shape is a coverage table for one report shape.
_READABLE_REPS = {
    # the 2026-07-28 range: 108 commits, 1 safe / 2 carried / 1 decided / 2 open
    "the real range": MAGIC_0728,
    # replayed: `render_md` leads with the REPLAYED banner instead of the ordinary
    # provenance line, above the same headline
    "replayed from cache": {**MAGIC_0728, "cached": True,
                            "replayed_at": "2026-07-28T05:32:00Z"},
    # nothing left to do — the daily report drops the two zeros for "nothing
    # outstanding", a SECOND phrasing that only `_RESOLVED_RE` reads
    "resolved": _rep(commit_count=3, carried=["a", "b"], decided=["c"]),
    # a range with no commits at all: every number is zero, in every document
    "empty range": _rep(commit_count=0),
    # every disclosure at once. The warnings are appended AFTER the counts in both the
    # report note and the PR line, so this is also the test that they do not push the
    # numbers out of the parser's reach.
    "every disclosure": _rep(commit_count=7, clearly_safe=["s"], carried=["c"],
                             decided=["d"], outstanding=["o", "p"], not_assessed=["n"],
                             unreachable=["u"], unconfirmed=["v"],
                             commits=[{"sha": "aaa", "decision": "human",
                                       "category": "bugfix", "recommend": "manual"}]),
    # counted from the ROWS, no summary lists (`summary_counts` step 2)
    "rows only": _rep(commit_count=5, commits=[
        {"sha": "a1", "decision": "auto-safe", "category": "bugfix"},
        {"sha": "a2", "decision": "carried", "category": "carried"},
        {"sha": "a3", "decision": "recorded:skip", "category": "decided"},
        {"sha": "a4", "decision": "human", "recommend": "manual", "category": "feature"},
        {"sha": "a5", "decision": "human", "recommend": "skip", "category": "ci"}],
        **{k: None for k in ("clearly_safe", "carried", "decided", "outstanding")}),
    # neither list nor row: the INFERRED banner renders above the table, and the numbers
    # it warns about still have to parse out of the headline it precedes
    "inferred": _rep(commit_count=9, commits=[],
                     **{k: None for k in ("clearly_safe", "carried", "decided",
                                          "outstanding")}),
}


def test_every_render_site_stays_readable_by_the_guard():
    """THE coverage assertion (vibeic/vibeic-eda#9).

    For every report that states counts, every document this program renders must give
    those counts back — so `cross_check` is guarding all three publications and not
    silently skipping one. Reword `render_md`, `assessment_entry` or `tally_line` without
    teaching `_HEADLINE_RE` and this goes red on the render itself.

    What it does NOT prove: that no FOURTH render site exists. Nothing here can see a
    site that was never added to `_RENDERS` — the kinds are pinned against the parser's
    own table below, which catches a new phrasing, not a new caller of an old one.
    """
    assert set(_RENDERS) == set(A._HEADLINE_RE), \
        "a phrasing the parser knows that no render in this table produces (or vice versa)"
    for name, rep in sorted(_READABLE_REPS.items()):
        assert A.states_counts(rep), name
        n = A.summary_counts(rep)
        want = {f: n[f] for f in A.HEADLINE}
        docs = {}
        for kind, render in sorted(_RENDERS.items()):
            text = render(rep)
            got = A.parse_headline(kind, text)
            assert got is not None, (
                f"{name}: the {kind} render states counts parse_headline cannot read — "
                f"a render and the guard have drifted apart: {text[:300]!r}")
            assert got == want, f"{name}/{kind}: {got} != {want}"
            docs[kind] = text
        # and the guard, handed all three at once, both READ and cleared them
        assert A.cross_check(rep, docs) == [], name


def test_a_tick_whose_assessment_is_reworded_publishes_neither():
    """End to end, on the production path: the guard is armed by the RENDER, so a render
    the parser cannot read must stop the tick.

    The injected wording states the CORRECT numbers, deliberately. #7 already catches a
    document that disagrees; what went unnoticed is a document that cannot be checked at
    all, and the fail-safe reading of that is "this day's triage is unverified" — not
    "the numbers were probably fine". The repair is to teach `_HEADLINE_RE` the new
    wording, which is precisely the work the silent skip used to excuse.
    """
    def _reworded(rep):
        n = A.summary_counts(rep)
        return (f"## {rep['tool']} — selective-merge assessment\n"
                f"Range **{rep['base_release']} → {rep['latest']}** · {n['commits']} "
                f"upstream commit(s).\n"
                f"{n['clearly_safe']} can be auto-adopted; {n['carried']} are carried, "
                f"{n['decided']} decided, and {n['outstanding']} need review.\n")

    assert A.parse_headline("assessment", _reworded(MAGIC_0728)) is None
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        try:
            _tick_fixture(state, MAGIC_0728, render=_reworded)
        except Exception as e:            # gatekeeper.CountsDisagree
            assert type(e).__name__ == "CountsDisagree", repr(e)
            assert "nothing published" in str(e)
        else:
            raise AssertionError("the tick published a document nothing could check")
        assert list((state / "reports").glob("*")) == [], "a report was published"
        assert not (state / "reports" / "assessments").exists(), \
            "an assessment was published that the gate could not read"
        led = json.loads((state / "ledger" / "magic.json").read_text())
        assert not led.get("sync_log"), "the ledger recorded a tick that never published"


def test_a_clean_fork_still_publishes_its_unparseable_row():
    """The common case, on the row #8 changed. A CLEAN row states no counts, and #8's
    `behind_commits` clause did not change that — it parses to None before and after —
    so a fix of the shape "fail every document that does not parse" would take a working
    tick down on most forks on most days. It publishes, and the row it publishes is a
    SKIP when handed to the guard.

    Two levels, and only the second is mutation-controlled: `tick` today hands the gate a
    row only on the ASSESSED branch, so no mutation of `cross_check` alone can make a
    CLEAN day fail. The published-row assertion below closes that by taking the route a
    future rewiring would take — the real clean report, the real published row.
    """
    led = {"tool": "Zeta", "integrated": True, "behind_releases": 0,
           "upstream": "them/Zeta", "image_version": "0.2.30", "base_release": "v2.2.0",
           "upstream_latest_release": "v2.2.0", "upstream_default_branch": "master",
           "behind_commits": 57}
    with tempfile.TemporaryDirectory() as t:
        state = Path(t)
        _, summary = _tick_fixture(state, None, ledgers={"Zeta": led})
        assert summary["counts"]["CLEAN"] == 1, summary["counts"]
        assert (state / "reports" / f"{summary['date']}.json").is_file(), \
            "a CLEAN day published nothing"
        assert json.loads((state / "ledger" / "Zeta.json").read_text())["sync_log"]
        row = next(r for r in summary["results"] if r["tool"] == "Zeta")
    assert "57 upstream commit(s) on master" in row["note"], row["note"]
    assert A.parse_headline("report", row["note"]) is None, \
        "#8's clause did not make a CLEAN row state the four counts"
    # the report `assess()` returns for such a fork, against the row the tick published
    clean = {"tool": "Zeta", "status": "clean", "commits": [],
             "base_release": "v2.2.0", "latest": "v2.2.0"}
    assert A.cross_check(clean, {"report": row["note"],
                                 "assessment": A.render_md(clean)}) == [], \
        "the published CLEAN row was failed for stating counts it never had"
# ── vibeic/vibeic-eda#10 — the configuration a tick runs on ─────────────────────
#
# For months the cron read a working-tree FORKS.json carrying three forks (OpenSTA,
# ALIGN-public, ALIGN-pdk-sky130) that no commit contained. The report was a faithful
# summary of a premise that existed on one machine; a fresh clone would have produced a
# 12-row report whose own headline count agreed with itself.


def _fc():
    return _load("fleet_config")


def _tick_and_read(state: Path, **kw):
    """Run a tick and return (summary, published markdown)."""
    gk, summary = _tick_fixture(state, _rep(commit_count=0), **kw)
    return gk, summary, (state / "reports" / f"{summary['date']}.md").read_text()


def test_a_hand_edited_fleet_list_refuses_the_tick_and_names_the_entries():
    """The defect itself. A fork added to the file on the box and to no commit must stop
    the tick — and the refusal must say WHICH forks, because "the fleet list differs"
    sends the operator back to diffing two files by hand, which is the state this check
    exists to end."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        hand_edited = {"org": "vibeic", "forks": [
            {"tool": "magic", "role": "test", "upstream": "them/magic"},
            {"tool": "OpenSTA", "role": "sta", "upstream": "The-OpenROAD-Project/OpenSTA"}]}
        try:
            _tick_fixture(state, _rep(commit_count=0), fleet_extra=hand_edited)
        except Exception as e:  # fleet_config.FleetConfigUnversioned
            assert type(e).__name__ == "FleetConfigUnversioned", repr(e)
            assert "OpenSTA" in str(e), str(e)
            assert "in no commit" in str(e), str(e)
            assert "nothing published" in str(e), str(e)
        else:
            raise AssertionError("the tick published from an uncommitted fleet list")
        assert list((state / "reports").glob("*")) == [] \
            if (state / "reports").exists() else True, "a report was published"


def test_the_refusal_names_a_changed_field_not_only_an_added_fork():
    """A `role` reworded on the box is not cosmetic: `assess_release` hands it to the
    judge as the context the classification is made in. A check that only counted
    entries would clear it."""
    fc = _fc()
    committed = {"org": "vibeic", "forks": [
        {"tool": "magic", "role": "DRC", "upstream": "them/magic"}]}
    on_disk = {"org": "vibeic", "forks": [
        {"tool": "magic", "role": "DRC / layout-edit", "upstream": "them/magic"}]}
    lines = fc.describe("FORKS.json", committed, on_disk)
    assert any("magic.role" in ln and "DRC" in ln and "layout-edit" in ln
               for ln in lines), lines
    # …and a repointed upstream, which changes which releases we are compared against
    moved = {"org": "vibeic", "forks": [
        {"tool": "magic", "role": "DRC", "upstream": "someone-else/magic"}]}
    assert any("magic.upstream" in ln for ln in fc.describe("FORKS.json", committed, moved))
    # a top-level key outside the fork list is reported too
    assert any("org" in ln for ln in
               fc.describe("FORKS.json", committed, {**committed, "org": "other"}))


def test_the_committed_state_does_not_fire():
    """The other half. A check that cannot pass is a check that gets deleted."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary, md = _tick_and_read(state)
        assert summary["fleet_config"]["state"] == "committed", summary["fleet_config"]
        assert summary["fleet_config"]["fatal"] == []
        assert "UNCOMMITTED" not in md and "UNVERSIONED" not in md, md


def test_the_report_names_the_configuration_that_produced_it():
    """vibeic/vibeic-eda#7 made the report name the ASSESSOR; the same row must name the
    CONFIGURATION. Two reports of one fleet produced from two fleet lists are otherwise
    indistinguishable — which is exactly how three untracked forks went unnoticed."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _, summary, md = _tick_and_read(state)
        sha = summary["fleet_config"]["commit"]
        assert sha and len(sha) == 40, summary["fleet_config"]
        assert sha[:12] in md, md
        assert "FORKS.json" in md and "committed" in md, md
        assert summary["fleet_config"]["entries"] == 1, summary["fleet_config"]
        # the stamp is above the counts: it is the premise they summarise
        assert md.index("FORKS.json") < md.index("**MERGED"), md


def test_a_report_whose_markdown_lost_the_configuration_stamp_is_caught():
    """The round trip. The JSON twin knowing which fleet list produced the day is no use
    to an operator reading the markdown; a formatter that drops the row leaves the
    document people actually open naming no configuration at all."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        gk, summary, md = _tick_and_read(state)
        assert gk.verify_documents(summary["date"]) == []
        stamp = gk.fleet_config.stamp_line(summary["fleet_config"])
        (state / "reports" / f"{summary['date']}.md").write_text(md.replace(stamp, ""))
        bad = gk.verify_documents(summary["date"])
        assert any("configuration stamp" in b for b in bad), bad


def test_a_report_written_before_the_stamp_existed_is_skipped_not_failed():
    """Every report published before 2026-07-28 carries no `fleet_config`. `--verify`
    must still be usable on them — a guard that fails on all its own history is one an
    operator stops running."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        gk, summary, md = _tick_and_read(state)
        rp = state / "reports" / f"{summary['date']}.json"
        old = {k: v for k, v in summary.items() if k != "fleet_config"}
        rp.write_text(json.dumps(old))
        (state / "reports" / f"{summary['date']}.md").write_text(
            md.replace(gk.fleet_config.stamp_line(summary["fleet_config"]), ""))
        assert gk.verify_documents(summary["date"]) == []


def test_formatting_only_drift_warns_and_publishes_rather_than_refusing():
    """A re-indented file changes no fork, no upstream and no role, so nothing audited
    can have changed. Killing the day's report over it teaches operators that this check
    is noise — and a check believed to be noise is a check that gets commented out."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        # the same document in different bytes: compact, keys reordered, no newline
        reindented = ('{"forks": [{"upstream": "them/magic", "role": "test", '
                      '"tool": "magic"}], "org": "vibeic"}')
        gk, summary = _tick_fixture(state, _rep(commit_count=0), fleet_extra=reindented)
        md = (state / "reports" / f"{summary['date']}.md").read_text()
        assert summary["fleet_config"]["state"] == "formatting", summary["fleet_config"]
        assert summary["fleet_config"]["fatal"] == []
        assert "UNCOMMITTED (formatting only)" in md, md


def test_a_source_tree_that_is_not_a_checkout_publishes_an_explicit_marker():
    """Nothing can vouch for the configuration, and refusing would break every
    deployment that is not a git checkout — the fastest route to the check being
    deleted. The report says so instead, in its header, permanently."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        src = state / "plain"
        src.mkdir()
        (src / "FORKS.json").write_text(json.dumps(
            {"org": "vibeic", "forks": [{"tool": "magic", "role": "t",
                                         "upstream": "them/magic"}]}))
        gk = _gk()
        was = gk.fleet_config.HERE
        try:
            gk.fleet_config.HERE = src
            st = gk.fleet_config.check()
            assert st["state"] == "unversioned", st
            assert st["fatal"] == [], st
            assert "UNVERSIONED" in gk.fleet_config.stamp_line(st)
        finally:
            gk.fleet_config.HERE = was


def test_the_override_publishes_but_cannot_buy_a_clean_report():
    """An escape hatch that leaves no mark is a slower way of deleting the check."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        hand_edited = {"org": "vibeic", "forks": [
            {"tool": "magic", "role": "test", "upstream": "them/magic"},
            {"tool": "OpenSTA", "role": "sta", "upstream": "them/OpenSTA"}]}
        os.environ[_fc().OVERRIDE_ENV] = "1"
        try:
            gk, summary = _tick_fixture(state, _rep(commit_count=0),
                                        fleet_extra=hand_edited)
        finally:
            os.environ.pop(_fc().OVERRIDE_ENV, None)
        md = (state / "reports" / f"{summary['date']}.md").read_text()
        assert summary["fleet_config"]["state"] == "modified", summary["fleet_config"]
        assert summary["fleet_config"]["override"] is True
        assert "UNCOMMITTED" in md and "GK_ALLOW_UNVERSIONED_FLEET" in md, md


def test_a_ledger_the_fleet_list_does_not_name_refuses_the_tick():
    """The half that keeps the stamp from being decoration. The report's rows come from
    the ledger directory, which `discover_forks.main` writes into and never prunes — so a
    fork dropped from the fleet list keeps publishing the last verdict anyone computed
    for it, frozen and indistinguishable from a live row. A report that names its fleet
    list in the header while carrying rows from outside it is a worse lie than one that
    names nothing."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        (state / "ledger").mkdir(parents=True)
        (state / "ledger" / "ghost.json").write_text(json.dumps(
            {"tool": "ghost", "integrated": True, "behind_releases": 0}))
        try:
            _tick_fixture(state, _rep(commit_count=0), ledgers={"magic": {
                "tool": "magic", "integrated": True, "behind_releases": 0}})
        except Exception as e:
            assert type(e).__name__ == "FleetConfigUnversioned", repr(e)
            assert "ghost" in str(e), str(e)
            assert "nothing published" in str(e), str(e)
        else:
            raise AssertionError("a row the fleet list does not authorise was published")
        assert list((state / "reports").glob("*.md")) == [] \
            if (state / "reports").exists() else True


def test_the_tick_never_writes_its_own_fleet_list():
    """vibeic/vibeic-eda#10 constraint 3. A tick that regenerated the list from discovery
    would take away the operator's ability to say "track this fork" and make every check
    above vacuous — a self-populating input cannot disagree with itself.

    Compared against the bytes `_pin_fleet` put there, so a tick that rewrites the file
    is caught even though the fixture would restore it before the next run."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        _tick_fixture(state, _rep(commit_count=0))
        assert json.loads((state / "_src" / "FORKS.json").read_text()) == {
            "org": "vibeic",
            "forks": [{"tool": "magic", "role": "test", "upstream": "them/magic"}]}, \
            "the tick rewrote its own fleet list — it must be an input, never an output"


def test_the_committed_fleet_list_carries_the_forks_the_box_was_tracking():
    """The regression guard for #10 itself. These three were live on the machine and in
    no commit; a checkout reset, a re-clone or a restore-from-remote would have dropped
    them silently, because the counts are derived from the file and the file is the
    ground truth for its own audit."""
    forks = json.loads((Path(__file__).parent / "FORKS.json").read_text())["forks"]
    tools = {f["tool"] for f in forks}
    for t in ("OpenSTA", "ALIGN-public", "ALIGN-pdk-sky130"):
        assert t in tools, f"{t} was tracked on the box and is missing from the commit"
    assert len(tools) == len(forks), "duplicate tool in the fleet list"
    # every fleet entry carries what the audit reads: where upstream is, and the role
    # the judge is given as context
    for f in forks:
        assert f.get("upstream") and f.get("role"), f


def test_the_fleet_list_and_the_published_page_describe_the_same_fleet():
    """`build_page` derives the monitor page's fork count from ENHANCEMENTS.json, not
    from the fleet list — so the two drifting apart publishes a count for a fleet nobody
    audits.

    This asserted EQUALITY until 778f9f1 (PR #15), which took FORKS.json from 15 to 21
    while the survey still covered 15. That is a legitimate state — a fork can be tracked
    before it is surveyed — and the landing repaired the PAGE for it: "all __NFORKS__
    forks" became "__NFORKS__ of the __NTOTAL__ forks" plus an explicit "the remaining
    __NUNSURVEYED__ forks carry no capability rows yet". What it did not repair was this
    test, which kept asserting the invariant the design had just replaced, and so has
    been RED on main since. Landing a design change that intentionally breaks a test
    without moving the test is the gate miss; deleting the test would have been the
    second one.

    So the invariant is replaced rather than relaxed, and the replacement is stronger in
    the direction that actually catches a defect:

      1. ENHANCEMENTS.json must be a SUBSET of the fleet. A survey row for a tool that
         is not tracked is a row nobody audits — the original failure, unchanged, still
         a hard fail.
      2. The surplus must be DISCLOSED, not merely tolerated. The page must substitute
         both counts and must not carry the "all N forks" quantifier, because a derived
         number inside a false quantifier is still a false sentence. Nothing pinned that
         repair before; it could have silently regressed on the next edit to GAP.
    """
    here = Path(__file__).parent
    fleet = {f["tool"] for f in json.loads((here / "FORKS.json").read_text())["forks"]}
    enh = set(json.loads((here / "ENHANCEMENTS.json").read_text()))
    assert enh <= fleet, {"surveyed but not tracked": sorted(enh - fleet)}

    gap = _load("build_page").GAP
    unsurveyed = sorted(fleet - enh)
    if unsurveyed:
        for token in ("__NTOTAL__", "__NUNSURVEYED__"):
            assert token in gap, (
                f"{len(unsurveyed)} tracked fork(s) are unsurveyed ({unsurveyed}) and "
                f"the page does not substitute {token}, so it cannot say so")
    assert "all __NFORKS__ forks" not in gap, \
        'the page claims to cover "all" forks while deriving the count from the ' \
        'surveyed subset — the count is right and the sentence is not'

# ── the ROLE is a judge input, so it is a cache input (vibeic/vibeic-eda#11) ──
# `assess_release` hands each fork's `role` to the judge as prompt context, and it was in
# neither thing that decides whether a cached verdict may be replayed: not in
# `_cache_key`, not in `assessor_id`'s ASSESSOR_SOURCES. Editing a role therefore changed
# what the judge is asked while every cached range replayed the verdict computed under
# the old wording — the same class as #4, which put the assessor in the key precisely so
# a changed judge cannot replay old verdicts.
#
# MEASURED at f90bf18, with the role moved from "DRC / layout-edit / extraction" to "DRC
# only — we do not use its extraction at all" and nothing else touched: the key was
# byte-identical (`magic|8.3.674|8.3.678|aaaaaaaaaaaa|3bad71411cf05a4f` both times), the
# second assess() returned `cached: True`, the judge was never asked the new question,
# and the commit stayed in `clearly_safe` — the tier that opens a real cherry-pick PR.
#
# WHICH MECHANISM, and why the narrow one. `assessor_id` is the identity of the thing
# that judges — one object, shared by the whole fleet, so moving it re-judges every fork,
# correctly. A `role` belongs to ONE fork. Hashing `FORKS.json` into `assessor_id` would
# make "a fork was added", or "OpenSTA's description was reworded", re-judge magic's
# cached range for a question that did not move. So the role rides in `_cache_key`, as a
# QUESTION component, and it is derived from `llm_judge.system_prompt` — the renderer the
# request itself uses — rather than from the config file, so that a field interpolated
# into the prompt later is picked up with no second edit.
#
# These tests stub only the HTTP layer; `classify_commits`, `llm_judge` and the
# confirmation round are the real ones.

def _role_fixture(tmp: Path, tools: dict, commits=None):
    """assess() over SEVERAL tools at once, each with its own ledger + role.

    Returns (assess_fn, sent, set_role). `sent` accumulates every outbound judge request
    body across calls — which is what makes "tool B was not re-judged" a measurement
    rather than an inference.
    """
    import importlib
    import urllib.request as U
    import llm_judge
    os.environ["GK_STATE_DIR"] = str(tmp)
    os.environ.pop("GK_ASSESS_STUB", None)
    importlib.reload(A)
    (tmp / "ledger").mkdir(parents=True, exist_ok=True)

    def set_role(tool, role):
        led = {"tool": tool, "integrated": True, "behind_releases": 1,
               "upstream": f"up/{tool}", "upstream_default_branch": "master",
               "pinned_ref_full": "a" * 40, "base_release": "8.3.674",
               "upstream_latest_release": "8.3.678"}
        if role is not None:
            led["role"] = role
        (tmp / "ledger" / f"{tool}.json").write_text(json.dumps(led))

    for tool, role in tools.items():
        set_role(tool, role)
    cs = commits or [{"sha": "sha000", "sha_full": "0" * 40, "title": "fix drc",
                      "body": "", "url": "", "author": "x"}]
    A.upstream_commits = lambda *a: (list(cs), ["f.c"])
    A.our_patch_files = lambda *a: set()
    A._commit_files = lambda *a: {"f.c"}
    A.clean_cherrypick = lambda *a: True
    A.already_carried = lambda *a: set()
    A.recorded_decisions = lambda *a: {}
    A._reachability = lambda *a: None
    sent = []
    llm_judge._token = lambda: "stub-token"

    def fake(req, timeout=None):
        """A judge that answers FROM THE ROLE it was handed — which is the whole premise:
        if the role were not an input to the verdict there would be nothing to key on."""
        body = json.loads(req.data.decode())
        sent.append(body)
        shas = _shas_in(body)
        role_text = body["system"][1]["text"]
        useful = set() if "we do not use" in role_text else set(shas)
        return _FakeResp(_mixed_reply(shas, useful))

    U.urlopen = fake
    return sent, set_role


def _teardown_role_fixture(orig_open, orig_tok):
    import importlib
    import urllib.request as U
    import llm_judge
    U.urlopen = orig_open
    llm_judge._token = orig_tok
    os.environ.pop("GK_STATE_DIR", None)
    importlib.reload(A)


def _role_env():
    import urllib.request as U
    import llm_judge
    return U.urlopen, llm_judge._token


R_WIDE = "DRC / layout-edit / extraction"
R_NARROW = "DRC only — we do not use its extraction at all"


def test_a_role_edit_is_a_different_question_and_must_not_replay():
    """THE DEFECT. Same tool, same upstream range, same carried-patch ref, same judge —
    a DIFFERENT `role`. The stored verdict must not be replayed, and the judge must be
    asked the NEW question."""
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, set_role = _role_fixture(tmp, {"magic": R_WIDE})
            r1 = A.assess("magic")
            assert not r1.get("cached")
            assert r1["clearly_safe"] == ["sha000"], r1["clearly_safe"]

            # unchanged role ⇒ still idempotent (the property #4 bought, kept)
            r2 = A.assess("magic")
            assert r2.get("cached") is True, "an unchanged role stopped hitting the cache"
            n_after_replay = len(sent)

            set_role("magic", R_NARROW)          # the ONLY thing that changes
            r3 = A.assess("magic")

            assert not r3.get("cached"), "a verdict computed under the OLD role was replayed"
            assert len(sent) > n_after_replay, "the judge was never asked the new question"
            assert R_NARROW in sent[-1]["system"][1]["text"], \
                "the re-judge did not carry the edited role"
            assert r3["clearly_safe"] == [], \
                "the verdict did not follow the role the judge was actually given"
            assert r3["judge_context"] != r1["judge_context"]
            assert r3["assessor"] == r1["assessor"], \
                "the JUDGE moved too — this test would then prove nothing about the role"
        finally:
            _teardown_role_fixture(*orig)


def test_editing_one_forks_role_does_not_invalidate_another_forks_cache():
    """NARROW SCOPE — the reason this is in `_cache_key` and not in `assessor_id`.

    Hashing `FORKS.json` into the assessor would re-judge the whole fleet on any fleet-list
    edit, including adding an unrelated fork. Only the fork whose question moved may miss.
    """
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, set_role = _role_fixture(tmp, {"magic": R_WIDE, "netgen": "LVS"})
            A.assess("magic")
            A.assess("netgen")
            assert A.assess("netgen").get("cached") is True     # settles into the cache
            aid_before = A.assessor_id()
            key_before = A._cache_key("netgen", "8.3.674", "8.3.678", "a" * 40,
                                      aid_before, A.judge_context_id("netgen", "LVS"))
            n = len(sent)

            set_role("magic", R_NARROW)          # a DIFFERENT fork's role
            assert not A.assess("magic").get("cached"), "the edited fork did not re-judge"
            spent_on_magic = len(sent) - n

            r = A.assess("netgen")
            assert r.get("cached") is True, \
                "editing magic's role invalidated netgen's cached verdict"
            assert len(sent) == n + spent_on_magic, \
                "netgen was re-judged over an edit to magic's role"
            assert A.assessor_id() == aid_before, \
                "the assessor moved on a fleet-list edit — that is the BROAD mechanism"
            assert A._cache_key("netgen", "8.3.674", "8.3.678", "a" * 40,
                                A.assessor_id(),
                                A.judge_context_id("netgen", "LVS")) == key_before
        finally:
            _teardown_role_fixture(*orig)


def test_the_key_assess_actually_writes_carries_the_question():
    """`_cache_key`'s `question` has a default so the key shape can be exercised without a
    judge import. A default is forgettable, so what is pinned is the key `assess()` really
    writes — end to end, out of the cache file — not the signature."""
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            _role_fixture(tmp, {"magic": R_WIDE})
            A.assess("magic")
            keys = list(json.loads((tmp / "assessment-cache" / "magic.json").read_text()))
            assert len(keys) == 1, keys
            parts = keys[0].split("|")
            assert len(parts) == 6, f"the written key has no question component: {keys[0]}"
            tool, base, new, our, question, assessor = parts
            assert (tool, base, new) == ("magic", "8.3.674", "8.3.678")
            assert question == A.judge_context_id("magic", R_WIDE), question
            assert question and question != assessor
            assert assessor == A.assessor_id()
        finally:
            _teardown_role_fixture(*orig)


def test_the_judge_request_and_the_cache_key_are_built_by_ONE_renderer():
    """`role` was missed because the invalidation set was "the judge's source files" while
    the prompt was assembled somewhere else. One renderer removes the gap: what the request
    carries and what the key hashes cannot drift apart."""
    import llm_judge
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, _ = _role_fixture(tmp, {"magic": R_WIDE})
            A.assess("magic")
            assert sent, "no request was made"
            assert sent[0]["system"] == llm_judge.system_prompt("magic", R_WIDE), \
                "the request does not come from the renderer the cache key hashes"
            assert R_WIDE in sent[0]["system"][1]["text"], "the role never reached the prompt"
        finally:
            _teardown_role_fixture(*orig)

    # DERIVED, not declared: the id follows the renderer's OUTPUT, so a value interpolated
    # into the prompt later is in the key with no second edit here.
    real = llm_judge.system_prompt
    base = A.judge_context_id("magic", R_WIDE)
    try:
        llm_judge.system_prompt = lambda tool, role: real(tool, role) + [
            {"type": "text", "text": "PDK: sky130A"}]      # a NEW data-derived field
        assert A.judge_context_id("magic", R_WIDE) != base, \
            "the question id ignored a value the renderer now puts in the prompt"
    finally:
        llm_judge.system_prompt = real
    assert A.judge_context_id("magic", R_WIDE) == base


def test_three_ledger_states_that_render_ONE_question_share_one_cache_entry():
    """`llm_judge` substitutes the literal "EDA tool" for a falsy role, so an absent role,
    `""` and "EDA tool" are three ledger states and ONE prompt. Keying on the raw string
    would re-judge a range whose question is byte-identical — which is why the id is over
    the RENDER."""
    import llm_judge
    ids = {A.judge_context_id("magic", r) for r in ("", "EDA tool")}
    ids.add(A.judge_context_id("magic", None or ""))
    assert len(ids) == 1, "three ledger states of one question got three cache slots"
    assert llm_judge.system_prompt("magic", "") == llm_judge.system_prompt("magic", "EDA tool")
    assert A.judge_context_id("magic", R_WIDE) not in ids, "a real role collided with the default"

    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, set_role = _role_fixture(tmp, {"magic": ""})
            A.assess("magic")
            n = len(sent)
            set_role("magic", None)                     # the key is absent entirely
            assert A.assess("magic").get("cached") is True, \
                "a role edit that does not move the PROMPT still re-judged"
            set_role("magic", "EDA tool")               # the literal the render falls back to
            assert A.assess("magic").get("cached") is True
            assert len(sent) == n, "the judge was called for a question that did not change"
        finally:
            _teardown_role_fixture(*orig)


def test_the_miss_names_the_ROLE_and_does_not_blame_the_judge():
    """Each widening re-judges every cached range once, and an unexplained spike in API
    calls is how a correct invalidation gets reverted. "The assessor changed", printed over
    a role edit, sends the reader to diff `llm_judge.py` and find it identical."""
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, set_role = _role_fixture(tmp, {"magic": R_WIDE})
            r1 = A.assess("magic")
            set_role("magic", R_NARROW)
            r2 = A.assess("magic")
            why = r2.get("reassessed_because", "")
            assert "judge context changed" in why, why
            assert "`role`" in why, why
            assert r1["judge_context"] in why and r2["judge_context"] in why, why
            assert "assessor changed" not in why, \
                "a role edit was reported as a changed judge"
        finally:
            _teardown_role_fixture(*orig)


def test_a_pre_role_cache_entry_is_rejudged_with_a_reason():
    """The LIVE cache at f90bf18 holds #4-shape keys (`…|<assessor>`, no question). They
    must not replay, and — because the assessor in them may well be the current one — the
    reason must say the KEY SHAPE widened, not that the judge changed."""
    orig = _role_env()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            sent, _ = _role_fixture(tmp, {"magic": R_WIDE})
            prefix = A._cache_input_prefix("magic", "8.3.674", "8.3.678", "a" * 40)
            (tmp / "assessment-cache").mkdir(parents=True, exist_ok=True)
            (tmp / "assessment-cache" / "magic.json").write_text(json.dumps({
                f"{prefix}|{A.assessor_id()}": {
                    "tool": "magic", "status": "assessed", "commit_count": 1,
                    "clearly_safe": ["a-stale-verdict"], "commits": []}}))
            r = A.assess("magic")
            assert not r.get("cached"), "a pre-#11 cache entry was replayed"
            assert r["clearly_safe"] == ["sha000"], "the stale verdict was served"
            why = r.get("reassessed_because", "")
            assert "before the judge context was part of the cache identity" in why, why
            assert "assessor changed" not in why, why
        finally:
            _teardown_role_fixture(*orig)


def test_the_other_data_derived_prompt_input_the_tool_name_cannot_replay_across_tools():
    """Found by the same execution sweep that found `role`: `{tool}` is interpolated into
    the system prompt twice. It is already the FIRST component of the key, so it cannot
    replay across tools — pinned here so the sweep's second finding has a guard too."""
    import llm_judge
    a = llm_judge.system_prompt("magic", R_WIDE)[1]["text"]
    b = llm_judge.system_prompt("netgen", R_WIDE)[1]["text"]
    assert a != b and "magic" in a and "netgen" in b, "the tool name is not in the prompt"
    k_magic = A._cache_key("magic", "8.3.674", "8.3.678", "a" * 40, "aid",
                           A.judge_context_id("magic", R_WIDE))
    k_netgen = A._cache_key("netgen", "8.3.674", "8.3.678", "a" * 40, "aid",
                            A.judge_context_id("netgen", R_WIDE))
    assert k_magic != k_netgen


def test_the_replay_banner_states_the_question_the_verdict_answers():
    """A replayed report claimed the range, the ref AND the assessor were unchanged — a
    completeness claim that was false while `role` could move underneath it."""
    rep = _provenance_rep(assessor="abc123abc123", judge_context="q0q0q0q0",
                          judge_role=R_WIDE, assessed_at="2026-07-25T05:00:00Z",
                          cached=True, replayed_at="2026-07-28T05:00:00Z")
    md = A.render_md(rep)
    assert "q0q0q0q0" in md, "a replayed verdict does not say which question it answers"
    assert R_WIDE in md
    assert "`role` puts to the judge" in md, \
        "the replay banner still claims completeness it does not have"
    assert A.parse_provenance(md).get("assessor") == "abc123abc123", \
        "the #7 provenance cross-check can no longer read the banner"

    # An archived report that predates the field must not be made to look as though it
    # recorded a question.
    old = A.render_md(_provenance_rep(assessor="abc123abc123",
                                      assessed_at="2026-07-25T05:00:00Z", cached=True))
    assert "asked as" not in old, "a report with no recorded question claimed one"
    assert "REPLAYED FROM CACHE" in old

    # The role is CONFIGURATION PROSE going into a markdown blockquote. A newline ends the
    # quote mid-sentence and a backtick opens a code span that swallows the rest of the
    # banner — including the words that say the verdict was not computed today.
    hostile = A.render_md(_provenance_rep(
        assessor="abc123abc123", judge_context="q0q0q0q0",
        judge_role="DRC\n\n## Not a heading\n`unclosed", cached=True,
        assessed_at="2026-07-25T05:00:00Z"))
    banner = [ln for ln in hostile.splitlines() if "REPLAYED FROM CACHE" in ln]
    assert len(banner) == 1 and banner[0].count("`") % 2 == 0, banner
    assert "Not a heading" in banner[0], "the role was dropped rather than flattened"
    assert "\n## " not in hostile.split("| sha |")[0].replace("\n## magic", ""), \
        "a role broke out of the provenance blockquote"


def test_the_fleet_list_is_deliberately_NOT_part_of_the_assessor_identity():
    """The design decision, pinned. `FORKS.json` in ASSESSOR_SOURCES would re-judge every
    fork on any fleet-list edit — including adding an unrelated fork — for questions that
    did not move. The per-fork question component is the narrower instrument."""
    assert not any(p.name == "FORKS.json" for p in A.ASSESSOR_SOURCES), A.ASSESSOR_SOURCES
    # and the instrument that DID take the role is per-fork, which is the whole argument
    assert A.judge_context_id("magic", R_WIDE) != A.judge_context_id("netgen", R_WIDE)


# ── vibeic/vibeic-eda#12 — one production state, writable by any process ──────────────
#
# `GK_STATE_DIR` defaulted to `~/.cache/eda-fork-gatekeeper` for EVERY checkout, so one
# cache, one ledger directory and one reports directory were writable by any process that
# imported these modules. On 2026-07-28 the cron ran 05:30:01→05:32:30 and
# `assessment-cache/magic.json` gained an entry stamped 07:07:21 written by code at
# 1b36787 — a non-cron process mutating the input the cron reads, recorded nowhere.
#
# Everything that landed that day sits downstream of that input: #4/#11 made the key a
# claim about which judge answered which question, #10 made the tick refuse an
# uncommitted fleet list, #7/#9 made it refuse documents that disagree. A poisoned entry
# does not make documents disagree — it makes them agree on the wrong thing.
#
# The tests below relocate the PRODUCTION constants to a throwaway directory. What is
# under test is the policy "is this the shared production location, and did this process
# say it is the production runner", not the literal path — and relocating is what lets the
# real refusal be exercised without going near the live cache the cron reads.

def _as_production(state=None, page=None, declared=False):
    import contextlib

    @contextlib.contextmanager
    def _cm():
        was = (GK.PRODUCTION_STATE, GK.PRODUCTION_PAGE, os.environ.get(GK.WRITER_ENV))
        if state is not None:
            GK.PRODUCTION_STATE = str(state)
        if page is not None:
            GK.PRODUCTION_PAGE = str(page)
        if declared:
            os.environ[GK.WRITER_ENV] = "1"
        else:
            os.environ.pop(GK.WRITER_ENV, None)
        try:
            yield
        finally:
            GK.PRODUCTION_STATE, GK.PRODUCTION_PAGE = was[0], was[1]
            os.environ.pop(GK.WRITER_ENV, None)
            if was[2] is not None:
                os.environ[GK.WRITER_ENV] = was[2]
    return _cm()


def test_a_non_production_process_cannot_write_the_shared_cache_by_default():
    """The 07:07 write, refused. `assess()` still runs and still returns its verdict —
    what it must not do is persist it into the state the cron reads."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod):
            try:
                _cache_fixture(prod, ["low"])
                rep = A.assess("magic")
                assert rep["clearly_safe"] == ["cc4da9a05fde"], "the run itself was broken"
                assert not (prod / "assessment-cache").exists(), \
                    "a non-production process wrote the production cache"
                # ...and it SAYS so. A silent no-op here is indistinguishable from a hit,
                # which is how a refusal gets mistaken for "nothing to do".
                why = rep.get("cache_write_refused") or ""
                assert "did not declare itself the production runner" in why, why
                assert GK.WRITER_ENV in why and "GK_STATE_DIR" in why, \
                    f"the refusal names no remedy: {why}"
                assert GK.PROVENANCE_KEY not in rep, \
                    "a refused run returned a report claiming it had written provenance"
            finally:
                _pop_state_dir()


def test_the_same_process_writes_the_shared_cache_when_it_asks():
    """NOT read-only for everyone (the explicit constraint on this fix). The distinction
    is production-runner vs everything else, and everything else may still write — it has
    to say so, and the entry then records that it did."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod, declared=True):
            try:
                _cache_fixture(prod, ["low"])
                rep = A.assess("magic")
                assert not rep.get("cache_write_refused"), rep.get("cache_write_refused")
                blob = json.loads((prod / "assessment-cache" / "magic.json").read_text())
                assert len(blob) == 1, blob
                stored = next(iter(blob.values()))
                assert stored[GK.PROVENANCE_KEY]["production"] is True
            finally:
                _pop_state_dir()


def test_a_state_dir_that_is_not_production_needs_no_permission():
    """The gate is on the LOCATION, not on writing. A run that owns its own state
    directory is unaffected — which is what keeps the remedy in the refusal message from
    being advice nobody can follow."""
    with tempfile.TemporaryDirectory() as d:
        prod, mine = Path(d) / "production", Path(d) / "mine"
        with _as_production(state=prod):
            try:
                _cache_fixture(mine, ["low"])
                rep = A.assess("magic")
                assert not rep.get("cache_write_refused"), rep.get("cache_write_refused")
                assert (mine / "assessment-cache" / "magic.json").is_file()
                assert not prod.exists(), "a scratch run reached the production directory"
            finally:
                _pop_state_dir()


def test_a_cache_entry_names_the_process_that_wrote_it():
    """The provenance the 07:07 entry lacked. `assessor` says which JUDGE answered;
    nothing said which checkout, which commit, or whether it was the cron — that was
    reconstructed from an mtime, which does not survive a copy and which no program can
    check."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod, declared=True):
            try:
                _cache_fixture(prod, ["low"])
                A.assess("magic")
                blob = json.loads((prod / "assessment-cache" / "magic.json").read_text())
                prov = next(iter(blob.values()))[GK.PROVENANCE_KEY]
                # every field is PRESENT even when undeterminable: a reader must be able
                # to tell "we could not find out" from "this shape predates the question"
                for k in ("at", "production", "entrypoint", "checkout", "commit",
                          "dirty", "pid", "host"):
                    assert k in prov, f"{k} missing from {sorted(prov)}"
                assert prov["checkout"] == str(GK.HERE)
                assert prov["pid"] == os.getpid()
                assert GK.describe(prov).startswith("the production runner")
            finally:
                _pop_state_dir()
    # and an entry from before provenance existed — the three live ones — must read as
    # UNKNOWN. "No block" defaulting to "the cron" would launder exactly the entry this
    # issue is about.
    legacy = GK.describe(None)
    assert "unknown" in legacy and "cron" not in legacy, legacy
    assert "NON-production" in GK.describe({"production": False}), GK.describe({})


def test_a_replayed_verdict_still_names_the_process_that_first_wrote_it():
    """Detectable NEXT time, not just at the moment of the write. A cached verdict is
    replayed for as long as the range and the judge hold still — #11 measured that as
    days — so the provenance has to be the ORIGINAL writer's, not the replayer's."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod, declared=True):
            try:
                _cache_fixture(prod, ["low", "medium"])
                first = A.assess("magic")
                second = A.assess("magic")
                assert second.get("cached") is True, "the replay path was not exercised"
                assert second[GK.PROVENANCE_KEY] == first[GK.PROVENANCE_KEY]
                # THE 07:07 ENTRY, reconstructed on disk and replayed. Overwriting the
                # stored block is what separates "the replay reports the STORED writer"
                # from "the replay manufactured one that happens to look right" — the two
                # are identical while the same process does both halves.
                p = prod / "assessment-cache" / "magic.json"
                blob = json.loads(p.read_text())
                key = next(iter(blob))
                blob[key][GK.PROVENANCE_KEY] = {
                    "at": "2026-07-27T23:07:21Z", "production": False,
                    "entrypoint": "assess_release.py", "checkout": "/some/other/checkout",
                    "commit": "1b36787", "dirty": True, "pid": 1, "host": "elsewhere"}
                p.write_text(json.dumps(blob, ensure_ascii=False))
                third = A.assess("magic")
                assert third.get("cached") is True
                said = GK.describe(third[GK.PROVENANCE_KEY])
                assert "NON-production" in said and "1b36787" in said, said
            finally:
                _pop_state_dir()


def test_a_tick_that_may_not_publish_refuses_before_it_spends_anything():
    """The preflight. A tick reaching the refusal at its WRITE would already have paid for
    a fleet-wide discovery and a judge call — so the check is ordered ahead of both, and
    ahead of the #10 configuration gate, because it needs neither network nor filesystem
    to answer."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod):
            os.environ["GK_STATE_DIR"] = str(prod)
            try:
                gk = _gk()
                spent = []

                def _boom(*a, **k):
                    spent.append("spent")
                    raise AssertionError("the tick spent something before the gate")

                gk.fleet_config = type("F", (), {"check": staticmethod(_boom)})()
                gk.disc = type("D", (), {"main": staticmethod(_boom)})()
                try:
                    gk.tick()
                except GK.ProductionStateWriteRefused as e:
                    assert "ledgers and reports" in str(e), str(e)
                else:
                    raise AssertionError("a non-production tick was allowed to publish")
                assert spent == [], "the gate ran after the spend"
                assert not prod.exists(), "the refused tick created production state"
            finally:
                os.environ.pop("GK_STATE_DIR", None)


def test_the_ledger_directory_is_production_state_too():
    """vibeic/vibeic-eda#10 proved a stale ledger publishes a frozen row indistinguishable
    from a live one, and `LEDGER.glob('*.json')` is what both the report and the public
    page iterate. Same exposure as the cache, so the same gate — before the first upstream
    call, for the same reason as the tick's preflight."""
    with tempfile.TemporaryDirectory() as d:
        prod = Path(d) / "production"
        with _as_production(state=prod):
            os.environ["GK_STATE_DIR"] = str(prod)
            try:
                disc = _load("discover_forks")
                called = []
                disc._gh_file = lambda *a, **k: called.append(a) or ""
                try:
                    disc.main()
                except GK.ProductionStateWriteRefused as e:
                    assert "ledgers" in str(e), str(e)
                else:
                    raise AssertionError("a non-production process re-seeded the ledgers")
                assert called == [], "discovery ran before the gate"
                assert not (prod / "ledger").exists()
            finally:
                os.environ.pop("GK_STATE_DIR", None)


def test_a_published_tick_stamps_every_document_with_the_process_that_wrote_it():
    """The ledger, the daily report and the assessment filed beside it all carry it. The
    2026-07-28 pair was two vintages of one date; #9 made them cross-check their COUNTS,
    and two documents can still agree while neither says who produced it."""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d)
        with _as_production(state=Path(d) / "elsewhere"):
            gk, summary = _tick_fixture(state, MAGIC_0728)
            date = summary["date"]
            report = json.loads((state / "reports" / f"{date}.json").read_text())
            led = json.loads((state / "ledger" / "magic.json").read_text())
            for name, doc in (("report", report), ("ledger", led)):
                prov = doc.get(GK.PROVENANCE_KEY)
                assert isinstance(prov, dict), f"{name} carries no provenance"
                assert prov["checkout"] == str(GK.HERE), name
                assert prov["production"] is False, \
                    f"{name} claims the production runner wrote it"


#: An inventory as `inventory.collect()` returns one, small enough to read and wide
#: enough to reach every branch of `render_inventory`: one row per fork state, a
#: duplicate upstream, and all three divergence channels populated.
_INV_STUB = {
    "image": "stub/vibeic-eda:0",
    "a": [
        {"dir": "yosys", "origin": "ours", "upstream": "YosysHQ/yosys", "state": "forked",
         "forks": ["yosys"], "used": True, "desc_en": "Synthesis", "desc_zh": "合成"},
        {"dir": "klayout", "origin": "ours", "upstream": "KLayout/klayout", "state": "forked",
         "forks": ["klayout", "klayout-2"], "used": True, "desc_en": "Layout", "desc_zh": "佈局"},
        {"dir": "cvc", "origin": "base", "upstream": "d-m-bailey/cvc", "state": "no",
         "forks": [], "used": False, "desc_en": "ERC", "desc_zh": "電性檢查"},
        {"dir": "volare", "origin": "base", "upstream": "", "state": "pip",
         "forks": [], "used": True, "desc_en": "PDK fetcher", "desc_zh": "PDK 下載"},
        {"dir": "bin", "origin": "base", "upstream": "", "state": "not-a-tool",
         "forks": [], "used": True, "desc_en": "PATH dir", "desc_zh": "PATH 目錄"},
        {"dir": "mystery", "origin": "base", "upstream": "", "state": "unknown-upstream",
         "forks": [], "used": False, "desc_en": "", "desc_zh": ""},
    ],
    "b": [{"tool": "xschem", "upstream": "StefanSchippers/xschem", "forks": [], "used": True,
           "desc_en": "Schematic capture", "desc_zh": "電路圖", "reason_en": "", "reason_zh": ""}],
    "c": [{"dir": "sky130A", "upstream": "RTimothyEdwards/open_pdks", "forks": ["open_pdks"],
           "desc_en": "Sky130 PDK", "desc_zh": "Sky130 製程包"}],
    "n_fork_repos": 3, "n_distinct_upstreams": 2,
    "dupes": {"klayout/klayout": ["klayout", "klayout-2"]},
    "missing_notes": ["mystery"], "stale_notes": ["removed-tool"],
    "unmeasured": ["could not list /foss/pdks in stub/vibeic-eda:0"],
}


def _offline_inventory(bp):
    """Stop `build()` from MEASURING the inventory in tests that are not about it.

    `build()` now calls `inventory.collect()`, which is ~50 live GitHub API calls (the
    org's repos, then one per fork for its parent, then the upstream metadata) plus a
    `docker run` per image. Measured when that landed: the two publish-boundary tests
    below went from 0.36s to 87.44s, and they assert nothing about the inventory — they
    guard what may cross into the published page. A unit test that spends the process's
    API budget to reach an assertion about provenance redaction is a test that will one
    day fail for a rate limit and be read as a redaction bug.

    So the measurement is stubbed here and the section gets its OWN test rather than
    being exercised as a side effect of an unrelated one.
    """
    bp.inventory = type("_StubInv", (), {"collect": staticmethod(
        lambda *_a, **_k: dict(_INV_STUB))})
    bp._image_ref = lambda: _INV_STUB["image"]
    return bp


def test_the_inventory_section_passes_text_not_markup_to__bi():
    """`_bi` escapes both of its arguments, so an HTML entity written by the caller is
    escaped twice and the reader sees the literal characters.

    This SHIPPED. Measured on https://vibeic.ai/eda-forks.html before the fix, three
    captions carried pre-escaped entities into `_bi` and the served HTML contained:

        &amp;quot;no&amp;quot;                &amp;quot;could not determine&amp;quot;
        &amp;quot;does not exist&amp;quot;     &amp;quot;which sky130A is this&amp;quot;
        &amp;quot;open_pdks produced it&amp;quot;   &amp;#39;s

    which a browser renders as `&quot;no&quot;` and `project&#39;s`. It is easy to
    reintroduce because the surrounding template is raw HTML where those entities are
    correct — the two conventions sit three lines apart in the same f-string.

    Asserted on the rendered output rather than on the source, so it also holds for a
    caption added later or for note text arriving from TOOL_NOTES.json.
    """
    bp = _load("build_page")
    html = bp.render_inventory(dict(_INV_STUB))
    for bad in ("&amp;quot;", "&amp;#39;", "&amp;amp;", "&amp;lt;", "&amp;gt;"):
        assert bad not in html, (
            f"{bad} in the rendered page: a caller passed an HTML entity to _bi, which "
            f"escapes it again and prints it as literal text")


def test_the_inventory_section_reports_every_divergence_it_finds():
    """The section's whole claim is that it AUDITS rather than describes: a directory
    with no note, a note for a directory that is gone, and anything it could not measure
    are reported instead of quietly contributing no row. Computing those three lists and
    dropping them would leave a page that looks complete and reads like an audit — the
    exact failure this replaced a pasted table to avoid — so the claim is asserted on the
    output, not trusted.

    The assertions name the REPORT's own wording, not just the tool. A first draft of
    this test asserted `"mystery" in html`, which passed with the whole divergence list
    deleted from the renderer — `mystery` is also an ordinary row in table A, so the
    check was measuring the row and reading it as the report. A test that stays green
    with the thing it guards removed is the defect it is supposed to catch, one level up.
    """
    bp = _load("build_page")
    html = bp.render_inventory(dict(_INV_STUB))
    assert "no note for image directory: mystery" in html, \
        "a directory with no note was not REPORTED (it may still render as a row)"
    assert "a note exists for 'removed-tool'" in html, \
        "a note for a directory not in the image was not reported"
    assert "could not list /foss/pdks" in html, "an unmeasured section did not say so"
    assert "absent, not empty" in html, \
        "the page did not say how to read a row affected by a failed measurement"
    # and the states that are NOT "no" stay distinguishable from it
    assert "pip-installed" in html and "upstream unconfirmed" in html, \
        "a state meaning 'could not determine' was collapsed into 'no'"
    assert "duplicates" in html, "duplicate forks of one upstream were counted as coverage"


def test_the_published_page_carries_no_provenance():
    """The publish boundary. `build_page` embeds whole ledger dicts and the whole latest
    report into the page served from vibeic.ai, so provenance — which names a local
    checkout path and a hostname — must come off there, exactly like the NDA redaction
    that already guards that boundary."""
    with tempfile.TemporaryDirectory() as d:
        state, out = Path(d) / "state", Path(d) / "site" / "eda-forks.html"
        out.parent.mkdir(parents=True)
        (state / "ledger").mkdir(parents=True)
        (state / "reports").mkdir(parents=True)
        prov = GK.provenance()
        (state / "ledger" / "magic.json").write_text(json.dumps(
            {"tool": "magic", "upstream": "them/magic", GK.PROVENANCE_KEY: prov}))
        (state / "reports" / "2026-07-28.json").write_text(json.dumps(
            {"date": "2026-07-28", "counts": {}, "results": [], GK.PROVENANCE_KEY: prov}))
        os.environ["GK_STATE_DIR"] = str(state)
        try:
            bp = _offline_inventory(_load("build_page"))
            bp.build(out)
            html = out.read_text()
            assert "magic" in html, "the page did not render the ledger at all"
            assert GK.PROVENANCE_KEY not in html, "provenance was published"
            assert str(GK.HERE) not in html, "a local checkout path was published"
            assert prov["host"] not in html, "the hostname was published"
        finally:
            os.environ.pop("GK_STATE_DIR", None)


def test_the_published_page_is_production_too():
    with tempfile.TemporaryDirectory() as d:
        state, page = Path(d) / "state", Path(d) / "site" / "eda-forks.html"
        (state / "ledger").mkdir(parents=True)
        page.parent.mkdir(parents=True)
        os.environ["GK_STATE_DIR"] = str(state)
        with _as_production(state=Path(d) / "elsewhere", page=page):
            try:
                bp = _offline_inventory(_load("build_page"))
                try:
                    bp.build(page)
                except GK.ProductionStateWriteRefused as e:
                    assert "monitor page" in str(e), str(e)
                    # the remedy has to be one that applies to THIS artefact: the page
                    # does not move because GK_STATE_DIR moved
                    assert "--out" in str(e) and "GK_STATE_DIR" not in str(e), str(e)
                else:
                    raise AssertionError("a non-production process republished the page")
                assert not page.exists()
                # ...and rendering one by hand somewhere else is untouched
                bp.build(Path(d) / "scratch.html")
                assert (Path(d) / "scratch.html").is_file()
            finally:
                os.environ.pop("GK_STATE_DIR", None)


def test_an_undeterminable_path_is_treated_as_production():
    """Fail CLOSED. Guessing "not production" silently poisons the cache, which is the
    defect; guessing "production" is a loud refusal with a one-line remedy."""
    was = GK.production_state_dir
    try:
        GK.production_state_dir = lambda: (_ for _ in ()).throw(RuntimeError("no HOME"))
        assert GK.is_production_path("/tmp/anywhere") is True
        os.environ[GK.WRITER_ENV] = "1"
        assert GK.may_write("/tmp/anywhere") is True, \
            "fail-closed became fail-shut: the declared production runner was refused"
    finally:
        GK.production_state_dir = was
        os.environ.pop(GK.WRITER_ENV, None)


def test_the_cron_reaches_the_same_state_dir_it_did_before_and_declares_itself():
    """THE regression that would silently stop the daily tick.

    Runs the REAL `run_tick.sh` — its own PATH/HOME resolution, its own flock, its own
    token lookup — with `python3` and `gh` stubbed on the PATH it builds for itself, and
    reads back the environment the tick process was actually handed. Asserting on the
    source text would pass against a script that no longer runs.
    """
    import subprocess
    script = Path(__file__).resolve().parent / "run_tick.sh"
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        # run_tick.sh OVERWRITES PATH with ${HOME}/.local/bin first, so that is where a
        # stub has to live for the script's own resolution to find it.
        binx = home / ".local" / "bin"
        binx.mkdir(parents=True)
        seen = home / "env.txt"
        (binx / "python3").write_text(
            f'#!/bin/sh\nprintf "%s\\n%s\\n" "$GK_STATE_DIR" "$GK_PRODUCTION_WRITER" '
            f'> "{seen}"\n')
        (binx / "gh").write_text('#!/bin/sh\necho gho_stub\n')
        for f in (binx / "python3", binx / "gh"):
            f.chmod(0o755)
        # PATH here only has to find `bash`; the script overwrites PATH with its own
        # (${HOME}/.local/bin first) before it resolves anything else.
        r = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                           timeout=120,
                           env={"HOME": str(home), "PATH": f"{binx}:/usr/bin:/bin"})
        assert r.returncode == 0, (r.returncode, r.stdout[-800:], r.stderr[-800:])
        state, writer = seen.read_text().splitlines()
        # SAME directory as before #12 — the cron's own resolution, not a re-derivation
        assert state == str(home / ".cache" / "eda-fork-gatekeeper"), state
        # ...and it is exactly the module's production location under that HOME
        was = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(home)
            assert Path(state) == GK.production_state_dir()
        finally:
            if was is not None:
                os.environ["HOME"] = was
        # ...and the tick process is told it is the production runner, so the same
        # directory stays WRITABLE to it.
        assert writer == "1", f"run_tick.sh did not declare itself: {writer!r}"
        was_w = os.environ.get(GK.WRITER_ENV)
        try:
            os.environ[GK.WRITER_ENV] = writer
            assert GK.may_write(Path(state) / "assessment-cache") is True
        finally:
            os.environ.pop(GK.WRITER_ENV, None)
            if was_w is not None:
                os.environ[GK.WRITER_ENV] = was_w


MODULES = ("gatekeeper", "discover_forks", "build_page", "prepare_merge_pr",
           "assess_release")


def test_every_module_resolves_state_through_the_one_policy():
    """Five modules each carried their own copy of the resolution line, which is how one
    of them gets hardened while the other four keep the hole open.

    Both halves are exercised by RUNNING each module's resolution, not by reading its
    source for a pattern: the override still wins, and — the half that catches an inlined
    default — relocating the POLICY's idea of the production location relocates all five.
    A module that still spelled `expanduser("~/.cache/eda-fork-gatekeeper")` itself would
    pass the first loop and fail the second.
    """
    with tempfile.TemporaryDirectory() as d:
        override, relocated = Path(d) / "override", Path(d) / "relocated"
        os.environ["GK_STATE_DIR"] = str(override)
        try:
            for name in MODULES:
                assert _load(name).STATE == override, name
        finally:
            os.environ.pop("GK_STATE_DIR", None)
        with _as_production(state=relocated):
            for name in MODULES:
                assert _load(name).STATE == relocated, \
                    f"{name} resolves its default without going through gk_state"


if __name__ == "__main__":
    # vibe-ic#395 sweep. `_cache_fixture` replaces five module attributes on
    # `assess_release` and restores NONE of them, so whichever test used it
    # last leaks its stubs into every test that runs after. In script order
    # (alphabetical) the victim was
    # `test_our_patch_files_unknown_on_error_fails_safe`: it called the leaked
    # `our_patch_files = lambda *a: set()` instead of the real function, so
    # the assertion that an ERRORED lookup returns None — the FAIL-SAFE the
    # conflict gate depends on — could not fail no matter what the production
    # code did. Under pytest the same suite passed, because pytest runs in
    # definition order and the leaking test happens to come after.
    #
    # A suite whose result depends on which runner you use is not reporting
    # on the code. Reload between tests so each starts from the real module;
    # `_cache_fixture` already reloads on entry, so this is the symmetric half
    # rather than a new convention.
    #
    # This block MUST stay the last thing in the file. It used to sit in the
    # middle, and `globals()` at that point does not contain the tests defined
    # below it — so script mode silently ran 20 of the 27 tests while pytest
    # ran all 27. Same file, two different answers about the code.
    import importlib
    fns = [k for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for name in fns:
        importlib.reload(A)
        globals()[name]()
        print(f"  ✓ {name}")
        passed += 1
    print(f"ALL {passed} PASS")


# --- owner ruling 2026-07-29: merge all upstream commits, not only releases ---

def test_a_fork_behind_by_commits_alone_is_a_candidate():
    """OWNER RULING (2026-07-29): "daily merge all new commits from upstream for
    forked tools." The gate used to read `behind_releases` alone, and the
    projects that matter most do not tag: OpenROAD's tags stop at v2.0, and
    yosys / verilator / iverilog / ngspice / cocotb / pyuvm / sby just move
    master. Their `behind_releases` is permanently 0, so they were NEVER
    candidates — not assessed and skipped, never entered.

    Measured the day this changed: 2 candidates out of 21 forks, while the
    never-entered ones were 1065 commits behind between them — 87% of the gap.
    That is how vibe-ic#551 happened: upstream fixed an `rsz::stitchTrees`
    segfault on 2026-07-13 and nothing here could see it.

    Asserted on the SOURCE of both gates rather than by driving `assess()`,
    because driving it needs a git fixture and this property is about a
    condition, not about a run. Both gates must read `behind_commits`; either
    one still reading releases alone reinstates the hole.
    """
    gk_src = Path(_gk().__file__).read_text()
    as_src = Path(A.__file__).read_text()
    for name, src in (("gatekeeper.py", gk_src), ("assess_release.py", as_src)):
        assert "behind_commits" in src, (
            f"{name} does not consider behind_commits; a fork behind a project "
            f"that cuts no releases would be invisible again")
    # The property is that the gate is an OR of BOTH counters, not that some
    # phrase is absent — my first version asserted the release phrase was gone,
    # which is wrong: the OR still contains it, so the assertion was vacuous in
    # one direction and false in the other.
    import re as _re
    gate = _re.search(r"behind = \((.{0,200}?)\n.{0,120}?\)", gk_src, _re.S)
    assert gate, "the candidate gate is no longer a named `behind` expression"
    body = gate.group(0)
    # The release side is now read through `release_gap()` — the one reader that
    # screens a null out of the arithmetic instead of coercing it to zero — so the
    # gate names the value it bound rather than the raw field. The property is
    # unchanged and still asserted: BOTH counters take part in the OR.
    assert ("behind_releases" in body or "_gap" in body), (
        f"the candidate gate no longer reads the release gap at all:\n{body}")
    assert "behind_commits" in body, (
        f"the candidate gate does not OR both counters:\n{body}")


def test_a_genuinely_level_fork_is_still_clean():
    """CONTROL. Without this the widened gate is indistinguishable from one that
    calls every fork a candidate, which would make the tick assess the whole
    fleet forever. Both counters at zero must still be clean."""
    as_src = Path(A.__file__).read_text()
    # `(led.get("behind_releases") or 0) == 0` is gone ON PURPOSE: it read a null
    # — a gap nobody could measure, or one there was nothing to measure — as a
    # measured zero, and this gate is exactly where that became a published
    # CLEAN. The clean gate still requires the release gap to be zero; it now
    # requires it to be a zero somebody measured.
    assert "rel_gap == 0" in as_src, (
        "the clean check no longer requires the release gap to be zero, or it "
        "stopped going through the one reader that screens out a null")
    assert '(led.get("behind_commits") or 0) == 0' in as_src, (
        "the clean check no longer requires commit distance to be zero too, so "
        "either every fork is dirty or the commit gap is ignored")


def test_an_assessment_that_returned_nothing_is_named_not_counted_as_fresh():
    """A None assessment is a HOLE, not a fresh one. It happens when the
    assessor is stubbed, times out, or raises after its key is set. Treating it
    as fresh crashes the tick; treating it as cached hides it. It must be
    dropped from both sets AND named, so a tool nobody assessed cannot read as
    a tool with nothing to assess."""
    src = Path(_gk().__file__).read_text()
    assert "NOT ASSESSED" in src, (
        "a None assessment is no longer named; a tool nobody assessed would "
        "read as one with nothing to assess")
    assert "not isinstance(r, dict)" in src


# ── a recommendation is not a decision (2026-08-04) ─────────────────────────

def test_a_recommended_skip_still_needs_a_human_decision():
    """`decision: human` + `recommend: skip` is OPEN, not settled.

    MEASURED on the 2026-08-04 tick, each file's headline against its own table:

        cocotb      17 claimed    61 rows marked human
        open_pdks    9 claimed    15 rows marked human
        slang        0 claimed     1 row  marked human

    51 commits the assessor itself declined to settle, absent from every summary a
    human reads. slang shows it at its clearest: the file announced that nothing
    needed review, so the tool would be skipped entirely, while its single commit
    was marked `human`.

    A settled skip exists and is spelled `recorded:skip` — counted under `decided`,
    which is why dropping the exclusion cannot double-count anything.
    """
    rows = [{"decision": "human", "recommend": "skip"},
            {"decision": "human", "recommend": "adopt"},
            {"decision": "recorded:skip"},
            {"decision": "auto-safe"}]
    n = A.summary_counts({"tool": "t", "status": "assessed",
                          "commit_count": 4, "commits": rows})
    assert n["outstanding"] == 2, (
        f"a recommended skip was dropped from the count that decides whether anyone "
        f"looks at this tool. got {n}")
    assert n["decided"] == 1, "a RECORDED skip is settled and stays settled"
    assert n["clearly_safe"] == 1


def test_the_headline_shows_the_split_without_hiding_the_total():
    """The exclusion was reaching for a real signal — what the assessor would adopt.

    That signal is kept, as a breakdown beside the honest total, so a reader can
    still triage without the skipped rows disappearing."""
    rows = [{"sha": "a", "decision": "human", "recommend": "skip",
             "category": "other", "summary": "", "title": ""},
            {"sha": "b", "decision": "human", "recommend": "adopt",
             "category": "bugfix", "summary": "", "title": ""}]
    md = A.render_md({"tool": "t", "status": "assessed", "base_release": "1",
                      "latest": "2", "our_patch_files": 0, "commit_count": 2,
                      "commits": rows})
    assert "needs human decision: 2" in md, md[:400]
    assert "1 the assessor would adopt" in md and "1 it would skip" in md, md[:400]

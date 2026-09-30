"""A fully published release failed on one misleading registry reply.

2026-09-30 05:30: `daily_release` published 0.3.88 end to end -- pushed, record
commit c533a53 on origin/main, `:latest` tagged, smoke 12/12.  The no-op
publication read-back then ran `docker manifest inspect` against the
`0.3.88` version tag and got rc != 0 with

    unsupported manifest format: &{{{2 application/vnd.oci.image.manifest.v1+json} ...

followed by the whole manifest printed as a Go struct: 85,893 characters on one
line.  `transient_failure_category` did not know the phrase, so the probe
answered UNKNOWN on its FIRST attempt, the release exited 1 with
ReleaseMeasurementUnknown(noop_publication, version_manifest_unmeasurable), and
run_tick exited 5.  Minutes later the same command returned rc 0 for 0.3.88 and
`:latest` twelve times in a row.

The phrase is the docker CLI's fallback, not a statement about the tag:
`manifest inspect` tries GetManifest (which, for an OCI image, also fetches the
config blob), discards ANY error from it, retries as a manifest LIST, and a
single image is not a list.  So the read-back must retry it -- and must still
answer exactly as before: only rc 0 is PRESENT, only an explicit not-found is
ABSENT, and an exhausted retry is UNKNOWN.

The reply below is the round's own (`daily-release.json`, unmeasured[0].detail
without the "registry manifest unmeasured: " prefix the exception adds): the
head and tail are verbatim, the middle is filled to the observed length.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import daily_release as R  # noqa: E402

VERSION = (HERE.parent / "VERSION").read_text().strip()
REF = "ghcr.io/vibeic/vibeic-eda:" + VERSION
LATEST = "ghcr.io/vibeic/vibeic-eda:latest"

_HEAD = (
    "unsupported manifest format: &{{{2 application/vnd.oci.image.manifest.v1+json}"
    " {application/vnd.oci.image.config.v1+json 84169 sha256:c17dc0ca9d0cc1bf5c122"
    "ae6570481b0720c2f0529cb99c6b0b7fbd8c9e478b4 [] map[] <nil>} [{application/vnd."
    "oci.image.layer.v1.tar+gzip 29732805 sha256:cb259a83ac3dd9fea0b394df41df2b298a"
    "df0df938fef5999475af18a751c257 [] map[] <nil>} {application/vnd.oci.image.laye"
    "r.v1.tar+gzip 71153 sha256:0f3f"
)
_TAIL = (
    "00 49 56 56 48 49 102 56 49 53 52 49 97 99 101 101 54 55 99 55 102 53 50 53 "
    "101 97 55 100 100 49 57 53 102 102 52 48 98 53 49 101 101 34 44 10 32 32 32 32 "
    "32 32 34 115 105 122 101 34 58 32 54 50 54 50 10 32 32 32 32 125 10 32 32 93 10"
    " 125]}"
)
OBSERVED_LEN = 85_893
OBSERVED = (_HEAD
            + (" 32" * OBSERVED_LEN)[:OBSERVED_LEN - len(_HEAD) - len(_TAIL)]
            + _TAIL)

NOT_FOUND = ("manifest unknown: manifest unknown")

MANIFEST = json.dumps({
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
               "size": 84169, "digest": "sha256:" + "c" * 64},
    "layers": [],
}, indent=2)
IDENTITY = hashlib.sha256(json.dumps(
    json.loads(MANIFEST), sort_keys=True, separators=(",", ":"),
    ensure_ascii=False).encode("utf-8")).hexdigest()


def _registry(monkeypatch, replies):
    """Fake `_sh`: `replies[ref]` is the list of (rc, out, err) to hand out,
    the last one repeating.  Returns the list of refs asked, in order."""
    asked = []

    def sh(cmd, **_kwargs):
        assert cmd[:3] == ["docker", "manifest", "inspect"], cmd
        ref = cmd[3]
        asked.append(ref)
        queue = replies[ref]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(R, "_sh", sh)
    return asked


# --- the classification ------------------------------------------------------

def test_the_observed_reply_is_a_transient_registry_read():
    """THE REGRESSION: this text was unclassified, so nothing retried it."""
    assert len(OBSERVED) == OBSERVED_LEN and "\n" not in OBSERVED
    assert R.transient_failure_category(OBSERVED) == "registry_manifest_unstable"
    # The form the record stored it in classifies the same way.
    assert R.transient_failure_category(
        "registry manifest unmeasured: " + OBSERVED) == "registry_manifest_unstable"
    # A network signature in the same text still names the network as cause.
    assert R.transient_failure_category(
        OBSERVED + "\nGet https://ghcr.io/v2/: net/http: TLS handshake timeout"
    ) == "network_timeout"


# --- the read-back: retried, and still fail-closed ----------------------------

def test_an_unstable_reply_then_a_manifest_is_present_with_identity(monkeypatch):
    sleeps = []
    asked = _registry(monkeypatch, {REF: [(1, "", OBSERVED), (0, MANIFEST, "")]})

    measured = R._probe_remote_manifest(REF, sleep=sleeps.append)

    assert measured.state is R.Presence.PRESENT
    assert measured.identity == IDENTITY
    assert asked == [REF, REF]
    assert sleeps == [2]


def test_an_unstable_reply_that_persists_is_unknown_after_five_attempts(
        monkeypatch):
    """Fail-closed: the wider window ends in UNKNOWN, never PRESENT or ABSENT."""
    sleeps = []
    asked = _registry(monkeypatch, {REF: [(1, "", OBSERVED)]})

    measured = R._probe_remote_manifest(REF, sleep=sleeps.append)

    assert measured.state is R.Presence.UNKNOWN
    assert measured.identity is None
    assert len(asked) == 5
    assert sleeps == [2, 10, 30, 60]


def test_a_not_found_reply_is_absent_at_once(monkeypatch):
    """Guard on meaning: an explicit absence is not retried into anything else."""
    sleeps = []
    asked = _registry(monkeypatch, {REF: [(1, "", NOT_FOUND)]})

    measured = R._probe_remote_manifest(REF, sleep=sleeps.append)

    assert measured.state is R.Presence.ABSENT
    assert asked == [REF]
    assert sleeps == []


def test_a_not_found_after_an_unstable_reply_is_unknown(monkeypatch):
    """A fetched manifest followed by absence is contradictory evidence."""
    sleeps = []
    asked = _registry(monkeypatch, {REF: [(1, "", OBSERVED), (1, "", NOT_FOUND)]})

    measured = R._probe_remote_manifest(REF, sleep=sleeps.append)

    assert measured.state is R.Presence.UNKNOWN
    assert asked == [REF, REF]
    assert sleeps == [2]


def test_the_publication_readback_survives_the_observed_reply(monkeypatch):
    """The 2026-09-30 sequence end to end: one bad reply for the version tag,
    then both tags read back as the same manifest -> verified, not UNKNOWN."""
    sleeps = []
    asked = _registry(monkeypatch, {
        REF: [(1, "", OBSERVED), (0, MANIFEST, "")],
        LATEST: [(0, MANIFEST, "")],
    })

    ok, evidence = R.verify_remote_release_publication(
        VERSION, probe=lambda ref: R._probe_remote_manifest(
            ref, sleep=sleeps.append))

    assert ok is True, evidence
    assert evidence["manifests"]["version"]["identity"] == IDENTITY
    assert evidence["manifests"]["latest"]["identity"] == IDENTITY
    assert asked == [REF, REF, LATEST]
    assert sleeps == [2]


# --- what an exhausted read-back stores ---------------------------------------

def test_an_exhausted_readback_stores_a_bounded_detail(monkeypatch, tmp_path):
    """The exception, the unmeasured[] entry and the failure summary each held
    all 85,893 characters.  They keep what failed and where it ended."""
    _registry(monkeypatch, {REF: [(1, "", OBSERVED)],
                            LATEST: [(0, MANIFEST, "")]})
    try:
        R.verify_remote_release_publication(
            VERSION, probe=lambda ref: R._probe_remote_manifest(
                ref, sleep=lambda _s: None))
    except R.ReleaseMeasurementUnknown as exc:
        raised = exc
    else:
        raise AssertionError("an exhausted read-back must stay UNKNOWN")

    assert raised.stage == "noop_publication"
    assert raised.category == "version_manifest_unmeasurable"
    assert raised.subject == REF
    assert len(raised.detail) <= 2000
    assert raised.detail.startswith(
        "registry manifest unmeasured: unsupported manifest format: &{{{2 ")
    assert raised.detail.endswith(_TAIL)
    assert "chars elided" in raised.detail

    record = tmp_path / "daily-release.json"
    result: dict = {}
    rc = R._finish_unmeasured(result, str(record), raised)
    stored = json.loads(record.read_text(encoding="utf-8"))

    assert rc == R.RC_NEEDS_HUMAN
    assert stored["status"] == "unmeasured"
    assert stored["failure"]["category"] == "version_manifest_unmeasurable"
    assert stored["failure"]["summary"] == raised.detail
    assert stored["unmeasured"][0]["detail"] == raised.detail
    assert record.stat().st_size < 10_000


def test_a_bounded_detail_keeps_both_ends_and_is_stable():
    text = "first line says what failed\n" + "x" * 50_000 + "\nlast words"
    bounded = R._bounded_detail(text)

    assert bounded.startswith("first line says what failed ... [")
    assert bounded.endswith("x" * 100 + "\nlast words")
    head, _, rest = bounded.partition(" ... [")
    elided = int(rest.split(" chars elided] ... ", 1)[0])
    assert len(head) + elided + R.DETAIL_TAIL_CHARS == len(text)
    # Bounding a bounded text changes nothing, so layered callers are safe.
    assert R._bounded_detail(bounded) == bounded
    # A short text is stored as it was.
    assert R._bounded_detail("TLS handshake timeout") == "TLS handshake timeout"


# --- the compose path shares the table, not the window -----------------------

def test_compose_keeps_its_window_for_a_readback_only_reply(tmp_path):
    """The phrase names a docker CLI fallback that only `manifest inspect` has.
    In a compose log it would be a manifest that really does not parse, so
    compose treats it exactly as before the category existed: unclassified, one
    defence-in-depth retry, not retryable -- not five attempts over 8 minutes."""
    assert R.transient_failure_category(OBSERVED) == "registry_manifest_unstable"
    runs, sleeps = [], []

    def run(cmd, **_kwargs):
        runs.append(cmd)
        return 1, "", "ERROR: failed to solve: " + OBSERVED

    rc, _err, history = R.compose_with_retries(
        ["docker", "buildx", "bake", "eda"], tmp_path, run=run,
        sleep=sleeps.append)

    assert rc == 1
    assert len(runs) == 2
    assert [h["category"] for h in history] == [None, None]
    assert sleeps == [R.COMPOSE_RETRY_DELAYS[0]]

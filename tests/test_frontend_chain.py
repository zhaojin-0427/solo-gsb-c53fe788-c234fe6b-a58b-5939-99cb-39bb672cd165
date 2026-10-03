"""Static guards for the high-precision playhead chain in static/app.js.

The browser code has no JS test runner in this repo, so these tests pin
the exact regressions that were fixed:

* a playhead time produced by play / drag / jump must travel from
  internal state and the jump input to
  ``GET /api/projects/{id}/eval?t=`` **verbatim** — no fixed-decimal
  rounding (the old ``round6``) may reappear in the input echo, the
  dedup key, the throttle trailing re-send, or the request URL itself;
* eval results are cached only per (project, data generation, instant):
  switching projects or successfully creating / updating / deleting an
  item must bump the generation, drop cached readouts and force a fresh
  eval at the same playhead instant; a response that arrives after the
  context changed must be discarded and never overwrite the current
  view;
* switching to a project (including an empty one) clears the previous
  project's channels immediately, and a failed eval of the current
  project clears the stale readouts; a failed save must touch neither.
"""

import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


def function_body(src: str, signature: str) -> str:
    """Return the body of a top-level JS function (closing brace at col 0)."""
    m = re.search(re.escape(signature) + r"\s*\{.*?\n\}", src, re.S)
    if not m:
        raise AssertionError(f"{signature} not found in app.js")
    return m.group(0)


class TestPlayheadPrecisionChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.flat = re.sub(r"\s+", " ", cls.src)

    def test_no_fixed_decimal_rounding_helper(self):
        # the microsecond-rounding helper is gone from the whole chain
        self.assertNotIn("round6", self.src)

    def test_jump_input_echo_is_verbatim(self):
        # the jump box echoes the exact playhead, so Go re-seeks to it
        self.assertIn('$("#jumpInput").value = String(state.time);', self.flat)

    def test_eval_query_uses_verbatim_state_time(self):
        # the request time is the unrounded playhead value
        self.assertIn("const tq = state.time;", self.flat)
        self.assertIn("/eval?t=${encodeURIComponent(String(tq))}", self.src)

    def test_dedup_compares_verbatim_time(self):
        # the cache key embeds the same unrounded value that would be sent,
        # so the instant part of the key is still lossless
        self.assertIn('const key = `${pid}|${gen}|${tq}`;', self.src)
        self.assertIn("if (state.readoutKey === key) return;", self.flat)

    def test_drag_end_resends_current_instant(self):
        body = function_body(self.src, "function startDrag(evt)")
        up = body.split("const up = () => {", 1)
        self.assertEqual(len(up), 2, "mouseup handler not found")
        self.assertIn("requestEval(true)", up[1])

    def test_pause_and_seek_resend_immediately(self):
        # every seek evaluates at once
        seek = function_body(self.src, "function seekTo(t)")
        self.assertIn("requestEval(true)", seek)
        # pause settles on the exact current instant
        play = function_body(self.src, "function setPlaying(on)")
        else_branch = play.split("} else {", 1)[1]
        self.assertIn("requestEval(true)", else_branch)

    def test_playback_throttle_still_present(self):
        # playback frames go through the rate limiter (immediate=false) and
        # a skipped frame schedules one trailing re-send of the live time
        tick = function_body(self.src, "function setPlaying(on)")
        self.assertIn("requestEval(false)", tick)
        self.assertIn("_evalTrailTimer", self.src)
        trail = re.search(
            r"_evalTrailTimer = setTimeout\(\(\) => \{(.*?)\}, 80\)",
            self.src, re.S)
        self.assertIsNotNone(trail, "throttle trailing timer not found")
        self.assertIn("requestEval(true)", trail.group(1))


class TestEvalDataGeneration(unittest.TestCase):
    """Same playhead instant must be re-evaluated once the data changes."""

    @classmethod
    def setUpClass(cls):
        cls.src = APP_JS.read_text(encoding="utf-8")
        cls.flat = re.sub(r"\s+", " ", cls.src)
        cls.req = function_body(cls.src, "async function requestEval(immediate)")
        cls.open = function_body(cls.src, "async function openProject(id)")
        cls.apply = function_body(
            cls.src, "async function applyProjectPayload(payload)")

    def test_generation_state_exists(self):
        self.assertIn("evalGen: 0", self.flat)
        self.assertIn("readoutKey: null", self.flat)
        # the old time-only dedup bookkeeping is gone
        self.assertNotIn("lastEvalTime", self.src)

    def test_cache_key_includes_project_generation_and_time(self):
        self.assertIn("const pid = state.projectId;", self.flat)
        self.assertIn("const gen = state.evalGen;", self.flat)
        self.assertIn('const key = `${pid}|${gen}|${tq}`;', self.src)

    def test_project_switch_bumps_generation(self):
        self.assertIn("state.evalGen += 1;", self.open)

    def test_data_mutation_bumps_generation(self):
        self.assertIn("state.evalGen += 1;", self.apply)

    def test_switch_clears_old_readouts_before_request(self):
        # old channels (incl. empty target project) must not linger
        self.assertIn("state.readouts = null;", self.open)
        self.assertIn("state.readoutKey = null;", self.open)
        clear_at = self.open.index("state.readouts = null;")
        req_at = self.open.index("requestEval(true)")
        self.assertLess(clear_at, req_at)
        # lanes are rebuilt against the new project before the clear/render
        self.assertLess(self.open.index("buildLanes();"), clear_at)

    def test_mutation_clears_old_readouts_before_request(self):
        self.assertIn("state.readouts = null;", self.apply)
        self.assertIn("state.readoutKey = null;", self.apply)
        clear_at = self.apply.index("state.readouts = null;")
        req_at = self.apply.index("requestEval(true)")
        self.assertLess(clear_at, req_at)

    def test_inflight_stale_success_is_discarded(self):
        # an in-flight response must be checked against the CURRENT context
        self.assertIn(
            "if (pid !== state.projectId || gen !== state.evalGen)",
            self.req)
        # locate the stale-success block (first occurrence of the guard)
        head = self.req.split(
            "if (pid !== state.projectId || gen !== state.evalGen)", 1)[1]
        block = head.split("return;", 1)[0]
        # ...must not overwrite state.readouts nor re-render...
        self.assertNotIn("state.readouts =", block)
        self.assertNotIn("renderReadouts()", block)
        # ...and must (conditionally) arrange a fresh eval of current data
        self.assertIn("state.evalQueued = true;", block)
        self.assertIn("state.readoutKey !==", block)
        # the current-instant key is rebuilt from live state
        self.assertIn(
            "`${state.projectId}|${state.evalGen}|${state.time}`", block)

    def test_inflight_stale_failure_ignored(self):
        # a stale request's failure must not wipe the newer context's
        # readouts nor toast its error
        m = re.search(
            r"catch \(e\) \{\s*//.*?if \(pid === state\.projectId &&\s*"
            r"gen === state\.evalGen\) \{(.*?)\}\s*\}",
            self.req, re.S)
        self.assertIsNotNone(m, "context-guarded catch block not found")
        live_block = m.group(1)
        # clearing/toasting only happens inside the current-context guard
        self.assertIn("state.readouts = null;", live_block)
        self.assertIn("state.readoutKey = null;", live_block)
        self.assertIn("renderReadouts();", live_block)
        self.assertIn("toast(e.message);", live_block)

    def test_stale_failure_arranges_rerun(self):
        # a stale failure must still leave a queued re-run so the current
        # data is evaluated (only when nothing in flight / cached covers it)
        self.assertIn(
            "(pid !== state.projectId || gen !== state.evalGen)", self.req)
        self.assertIn("!state.evalQueued", self.req)
        # exactly three queue sites: single-flight, stale success, stale
        # failure — no other path may queue silently
        self.assertEqual(self.req.count("state.evalQueued = true;"), 3)

    def test_inflight_check_precedes_cache_reuse(self):
        # otherwise a same-key response in flight could be dedup-skipped and
        # its staleness would never trigger a compensating request
        inflight = self.req.index("if (state.evalInflight)")
        reuse = self.req.index("if (state.readoutKey === key) return;")
        self.assertLess(inflight, reuse)

    def test_queued_rerun_reads_current_context(self):
        # the single-flight re-run goes through requestEval again, which
        # re-reads projectId/evalGen/time (no captured stale values)
        tail = self.req.split("if (state.evalQueued) {", 1)[1]
        self.assertIn("requestEval(true);", tail)

    def test_failed_save_keeps_readouts(self):
        # saveForm's error path shows the form error and returns without
        # applying the payload / touching eval state (take the LAST catch)
        save = function_body(self.src, "async function saveForm(form, isEdit, errBox)")
        err = save.rsplit("} catch (e) {", 1)[1]
        self.assertNotIn("applyProjectPayload", err)
        self.assertNotIn("evalGen", err)
        self.assertNotIn("readouts = null", err)


if __name__ == "__main__":
    unittest.main()

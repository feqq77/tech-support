"""Failure reports must name the exact step, cause and environment - always."""

from __future__ import annotations

import os

import pytest

from app import diagnostics
from app.diagnostics import StageTrace, build_report, probe_environment, save_report


def _trace_with_failure() -> StageTrace:
    trace = StageTrace()
    with trace.stage("create the WebRTC peer connection", "set up ICE, DTLS and SCTP"):
        pass
    try:
        with trace.stage(
            "open the input data channel",
            "pick the SCTP port and open the channel the technician types into",
        ):
            raise OSError(1, "Operation not permitted")
    except OSError:
        pass  # recorded on the step, exactly like the real failure path
    return trace


def test_stage_trace_records_steps_and_errors():
    trace = _trace_with_failure()
    assert [s["name"] for s in trace.steps] == [
        "create the WebRTC peer connection",
        "open the input data channel",
    ]
    assert trace.steps[0]["error"] is None
    assert "Operation not permitted" in trace.steps[1]["error"]


def test_report_names_the_failing_step_and_error():
    exc = OSError(1, "Operation not permitted")
    try:
        raise exc
    except OSError as raised:
        report = build_report(raised, _trace_with_failure(), context="test context")

    assert "FAILING STEP   : open the input data channel" in report
    assert "WHAT IT DOES   : pick the SCTP port" in report
    assert "ERRNO          : 1 (EPERM)" in report
    assert "Operation not permitted" in report
    assert "CAPABILITY PROBES" in report
    assert "WHAT THIS MEANS" in report
    assert "app version   : " in report
    assert "context       : test context" in report
    # the traceback is in there, not just the message
    assert "Traceback" in report


def test_probes_never_raise_even_when_everything_is_denied(monkeypatch):
    from app import randshim

    def denied(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(randshim, "_real_urandom", denied)
    monkeypatch.setattr(randshim, "_real_getrandom", denied)
    probes = probe_environment()  # must not raise
    by_label = {p.label: p for p in probes}
    assert by_label["entropy.kernel_urandom"].status == "FAIL"
    assert "Operation not permitted" in by_label["entropy.kernel_urandom"].detail


def test_interpretation_says_entropy_when_entropy_is_denied(monkeypatch):
    from app import randshim

    def denied(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(randshim, "_real_urandom", denied)
    report = build_report(OSError(1, "Operation not permitted"), _trace_with_failure())
    flat = " ".join(report.split())  # the plain-English notes are wrapped
    assert "DENIES the kernel random-number source" in flat
    assert "NOT a file-permissions problem" in flat


def test_report_is_saved_to_a_readable_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_HOME", str(tmp_path))
    path = save_report("hello report")
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == "hello report"
    assert os.path.basename(path) == "last-error.txt"

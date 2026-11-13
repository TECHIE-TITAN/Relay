from __future__ import annotations

import json
from datetime import datetime, timezone

import yaml

from relay.cli import main

SCEN = {"defaults": {"source": "real", "setting": "S1", "limits_scale": 1, "kind": "sequential", "max_requests": 1,
                     "deadline_s": 10, "gateway": {"pilot_token_cap": 30000}, "pilot": {"max_actual_per_call": 6000}},
        "scenarios": {"study": {"description": "t"}}}


def prepare(project, used):
    (project / "results").mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (project / "results" / "ledger_test_model.json").write_text(
        json.dumps({"date": today, "tokens": used, "requests": 10}), encoding="utf-8")
    f = project / "scen.yaml"
    f.write_text(yaml.safe_dump(SCEN), encoding="utf-8")
    return f


def test_ledger_command_shows_what_is_left(project, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    prepare(project, 100000)
    assert main(["ledger"]) == 0
    out = capsys.readouterr().out
    assert "100000 / 160000" in out and "60000 left" in out


def test_a_run_whose_cap_does_not_fit_the_ledger_is_refused_before_anything_is_sent(project, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = prepare(project, 150000)                                   # 10K left, the scenario wants 30K
    assert main(["run", "--scenario", "study", "--scenarios-file", str(f), "--allow-real"]) == 2
    err = capsys.readouterr().err
    assert "does not fit" in err and "--token-cap 10000" in err and "Nothing was sent" in err
    assert not (project / "results" / "runs").exists()             # no run folder was even created


def test_token_cap_can_lower_a_cap_so_the_run_fits_but_never_raise_it(project, monkeypatch, capsys):
    monkeypatch.setenv("RELAY_ROOT", str(project))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    f = prepare(project, 150000)
    # lowered to 8000: it fits, so the ledger check passes and the run goes on, then stops for another reason
    # (this tiny project has no chunk files and no API key), still before anything could be sent
    assert main(["run", "--scenario", "study", "--scenarios-file", str(f), "--allow-real", "--token-cap", "8000"]) == 2
    captured = capsys.readouterr()
    assert "worst case after the run 158000" in captured.out and "does not fit" not in captured.err
    # asking for MORE than the scenario's 30000 does not raise it, so it is still refused
    assert main(["run", "--scenario", "study", "--scenarios-file", str(f), "--allow-real", "--token-cap", "90000"]) == 2
    assert "does not fit" in capsys.readouterr().err

from pathlib import Path


HERE = Path(__file__).resolve().parent
TICK = (HERE / "run_tick.sh").read_text(encoding="utf-8")
ROUND = (HERE / "run_0530.sh").read_text(encoding="utf-8")


def test_vendored_closure_runs_between_merge_and_selftests():
    merge = TICK.index('python3 "${DIR}/daily_merge.py"')
    closure = TICK.index('python3 "${DIR}/vendored_dependency_closure.py"')
    selftest = TICK.index('SELFTEST_ARGS=()')
    release = TICK.index('python3 "${DIR}/daily_release.py"')
    assert merge < closure < selftest < release


def test_updated_host_cannot_reuse_preclosure_selftest_evidence():
    assert 'closure.get("updated_hosts")' in TICK
    assert 'and tool not in closure_updated' in TICK
    assert '[ "${vendored_rc}" != "0" ] && blocking_rc=1' in TICK
    assert '[ "${vendored_rc:-0}" != "0" ]' in TICK


def test_valid_but_nonconverged_ll_cannot_exit_zero_or_publish():
    capture = ROUND.index('capture_release_ll.py')
    terminal = ROUND.index('check_release_ll_terminal.py')
    page = ROUND.index('python3 "${DIR}/build_page.py"')
    assert capture < terminal < page
    assert '[ "${TERMINAL}" -eq 0 ]' in ROUND
    final = ROUND[ROUND.rindex('# 0 only when BOTH are clean:'):]
    assert '[ "${TERMINAL}" -ne 0 ]' in final


def test_certified_page_is_persisted_by_an_isolated_publisher():
    build = ROUND.index('python3 "${DIR}/build_page.py"')
    publish = ROUND.index('python3 "${DIR}/publish_site_page.py"')
    silence = ROUND.index('python3 "${DIR}/check_publish_silence.py"')
    assert build < publish < silence
    assert '--page eda-forks.html --branch main' in ROUND

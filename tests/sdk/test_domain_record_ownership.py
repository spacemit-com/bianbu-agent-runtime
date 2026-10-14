from __future__ import annotations


def test_session_backends_share_the_domain_owned_record_class() -> None:
    from bianbu_agent.sessions import StoredSession
    from bianbu_agent.sessions.models import StoredSession as DomainStoredSession
    from bianbu_agent.sessions.stores.sqlite import StoredSession as SQLiteStoredSession

    assert StoredSession is DomainStoredSession
    assert SQLiteStoredSession is DomainStoredSession


def test_run_backends_share_the_domain_owned_record_class() -> None:
    from bianbu_agent.runs import StoredRun
    from bianbu_agent.runs.models import StoredRun as DomainStoredRun
    from bianbu_agent.runs.stores.sqlite import StoredRun as SQLiteStoredRun

    assert StoredRun is DomainStoredRun
    assert SQLiteStoredRun is DomainStoredRun

from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from portfolio_tracker import web
from portfolio_tracker.db import Database
from portfolio_tracker.research import add_evidence, content_hash, run_research


def setup_client(monkeypatch, tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    monkeypatch.setattr(web, "Database", lambda: db)
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    client = web.app.test_client()
    assert client.post("/login", data={"username": "admin", "password": "secret"}).status_code == 302
    return client, db


def test_no_fabrication_and_stale_evidence(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    unavailable = run_research(db, topic="news", facts=[])
    assert unavailable["status"] == "unavailable"
    assert unavailable["external_call"] is False
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    evidence_id = add_evidence(db, {"source": "manual", "source_identifier": "doc-1", "captured_at": old, "provider": "manual", "excerpt": "old", "topic": "news", "verified": True})
    stale = run_research(db, topic="news", facts=[{"evidence_id": evidence_id, "statement": "must not be used"}], max_age_seconds=60)
    assert stale["status"] == "unavailable"
    assert "stale" in stale["rejections"][0]


def test_provenance_feedback_and_paper_gate(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    evidence_id = add_evidence(db, {"source": "provider", "source_identifier": "url-1", "captured_at": datetime.now(timezone.utc).isoformat(), "provider": "kite", "excerpt": "verified fact", "symbol": "INFY", "topic": "market", "verified": True})
    result = run_research(db, topic="market", facts=[{"evidence_id": evidence_id, "symbol": "INFY", "statement": "supplied fact"}])
    finding_id = result["findings"][0]["id"]
    assert result["findings"][0]["evidence_ids"] == [evidence_id]
    db.add_research_feedback(finding_id=finding_id, action="accepted", user_id="admin")
    assert db.list_research_findings()[0]["status"] == "accepted"


def test_future_dated_evidence_cannot_be_added_as_current(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        add_evidence(db, {"source": "filing", "source_identifier": "future", "captured_at": future,
                          "excerpt": "not yet published", "symbol": "INFY", "verified": True})


def test_authenticated_research_api_and_live_rejection(tmp_path, monkeypatch):
    client, db = setup_client(monkeypatch, tmp_path)
    assert client.get("/api/research/news").get_json()["external_call"] is False
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    response = client.post("/api/research/run", json={"topic": "market", "facts": []}, headers=csrf)
    assert response.status_code == 200
    assert response.get_json()["status"] == "unavailable"
    live = client.post("/api/orders/paper", json={"mode": "live"}, headers=csrf)
    assert live.status_code == 409
    assert db.list_research_runs()


def test_public_evidence_api_cannot_self_certify(tmp_path, monkeypatch):
    client, db = setup_client(monkeypatch, tmp_path)
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    response = client.post("/api/research/evidence", json={
        "source": "manual", "source_identifier": "operator-claim",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "excerpt": "Operator-supplied text", "symbol": "ABC", "topic": "market",
        "verified": True,
    }, headers=csrf)
    assert response.status_code == 201
    assert response.get_json()["evidence"]["verified"] in (0, False)
    assert db.list_research_evidence()[0]["verified"] == 0


def test_evidence_api_returns_the_inserted_unverified_record(tmp_path, monkeypatch):
    client, db = setup_client(monkeypatch, tmp_path)
    db.init_db()
    add_evidence(db, {
        "source": "filing", "source_identifier": "newer-trusted",
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "excerpt": "Older trusted record", "symbol": "ABC", "topic": "market", "verified": True,
    })
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    response = client.post("/api/research/evidence", json={
        "source": "filing", "source_identifier": "new-unverified",
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "excerpt": "This inserted item is unverified", "symbol": "ABC", "topic": "market",
        "verified": True,
    }, headers=csrf)
    assert response.status_code == 201
    evidence = response.get_json()["evidence"]
    assert evidence["source_identifier"] == "new-unverified"
    assert evidence["verified"] == 0


def test_new_evidence_defaults_unverified(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    evidence_id = add_evidence(db, {
        "source": "manual", "source_identifier": "unattested",
        "captured_at": datetime.now(timezone.utc).isoformat(), "excerpt": "Not externally checked",
    })
    assert db.list_research_evidence()[0]["id"] == evidence_id
    assert db.list_research_evidence()[0]["verified"] == 0


def test_research_uses_stored_excerpt_and_rejects_claim_scope_mismatches(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    evidence_id = add_evidence(db, {
        "source": "filing", "source_identifier": "doc", "captured_at": datetime.now(timezone.utc).isoformat(),
        "excerpt": "Stored excerpt", "symbol": "ABC", "topic": "market", "verified": True,
    })
    result = run_research(db, topic="market", facts=[
        {"evidence_id": evidence_id, "symbol": "OTHER", "statement": "fabricated caller claim"},
        {"evidence_id": evidence_id, "topic": "earnings", "statement": "also fabricated"},
        {"evidence_id": evidence_id, "symbol": "ABC", "statement": "caller claim"},
    ])
    assert result["verified_fact_count"] == 1
    assert result["rejected_fact_count"] == 2
    assert result["findings"][0]["symbol"] == "ABC"
    assert result["findings"][0]["statement"] == "Stored excerpt"


@pytest.mark.parametrize("max_age", [True, False, -1, 86401, 1.5, "86400"])
def test_research_rejects_invalid_freshness_limits(tmp_path, max_age):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    with pytest.raises(ValueError, match="max_age_seconds"):
        run_research(db, topic="market", max_age_seconds=max_age)


def test_research_api_rejects_invalid_freshness_limits(tmp_path, monkeypatch):
    client, _ = setup_client(monkeypatch, tmp_path)
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    for value in (True, -1, 86401, 1.5, "86400"):
        response = client.post("/api/research/run", json={"topic": "market", "max_age_seconds": value}, headers=csrf)
        assert response.status_code == 400


def test_add_evidence_rejects_caller_supplied_content_hash_mismatch(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    with pytest.raises(ValueError, match="content_hash"):
        add_evidence(db, {
            "source": "filing", "source_identifier": "doc",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "excerpt": "actual excerpt", "content_hash": "0" * 64,
        })


def test_long_evidence_hash_matches_exact_stored_excerpt(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    excerpt = "A" * 4000 + "different suffix"
    evidence_id = add_evidence(db, {"source": "filing", "source_identifier": "long-doc",
        "captured_at": datetime.now(timezone.utc).isoformat(), "excerpt": excerpt,
        "content_hash": content_hash(excerpt)})
    stored = db.get_research_evidence(evidence_id)
    assert stored is not None
    assert stored["excerpt"] == excerpt[:4000]
    assert stored["content_hash"] == content_hash(stored["excerpt"])


@pytest.mark.parametrize("facts", [[None], ["not an object"], [1]])
def test_run_research_rejects_non_object_facts_without_crashing(tmp_path, facts):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    with pytest.raises(ValueError, match="facts"):
        run_research(db, topic="market", facts=facts)


def test_run_research_rejects_malformed_evidence_id_in_fact_object(tmp_path):
    db = Database(tmp_path / "research.sqlite3")
    db.init_db()
    add_evidence(db, {
        "source": "manual", "source_identifier": "doc",
        "captured_at": datetime.now(timezone.utc).isoformat(), "excerpt": "evidence",
        "topic": "market", "verified": True,
    })
    result = run_research(db, topic="market", facts=[{"evidence_id": {"bad": 1}}])
    assert result["status"] == "unavailable"
    assert result["rejected_fact_count"] == 1


@pytest.mark.parametrize("facts", [[None], ["not an object"], [1], "not an array"])
def test_research_api_rejects_malformed_facts(tmp_path, monkeypatch, facts):
    client, _ = setup_client(monkeypatch, tmp_path)
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    response = client.post("/api/research/run", json={"topic": "market", "facts": facts}, headers=csrf)
    assert response.status_code == 400


@pytest.mark.parametrize("body", [["x"], "x", 1, True])
def test_research_run_api_rejects_non_object_json_root(tmp_path, monkeypatch, body):
    client, _ = setup_client(monkeypatch, tmp_path)
    with client.session_transaction() as sess:
        csrf = {"X-CSRF-Token": sess["csrf_token"]}
    response = client.post("/api/research/run", json=body, headers=csrf)
    assert response.status_code == 400


@pytest.mark.parametrize("route", ("evidence", "runs", "findings"))
def test_research_listing_rejects_malformed_limit(tmp_path, monkeypatch, route):
    client, _ = setup_client(monkeypatch, tmp_path)
    response = client.get(f"/api/research/{route}?limit=bad")
    assert response.status_code == 400


def test_research_findings_rejects_malformed_run_id(tmp_path, monkeypatch):
    client, _ = setup_client(monkeypatch, tmp_path)
    response = client.get("/api/research/findings?run_id=bad")
    assert response.status_code == 400


def test_login_redirect_accepts_only_local_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("PORTFOLIO_ADMIN_PASSWORD_HASH", generate_password_hash("secret"))
    client = web.app.test_client()
    for unsafe_next in ("https://attacker.example/path", "//attacker.example/path"):
        response = client.post(f"/login?next={unsafe_next}", data={"username": "admin", "password": "secret"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
    response = client.post("/login?next=%2Fpage%2Fstock-analysis%3Fsymbol%3DABC", data={
        "username": "admin", "password": "secret",
    })
    assert response.status_code == 302
    assert response.headers["Location"] == "/page/stock-analysis?symbol=ABC"

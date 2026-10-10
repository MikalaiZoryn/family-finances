import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "plaid_link", Path(__file__).parents[2] / "scripts" / "plaid_link.py"
)
plaid_link = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plaid_link)


def test_finished_session_picks_latest_finished():
    response = {
        "link_sessions": [
            {"link_session_id": "a", "finished_at": "2026-10-10T10:00:00Z"},
            {"link_session_id": "b", "started_at": "2026-10-10T11:00:00Z"},
            {"link_session_id": "c", "finished_at": "2026-10-10T10:05:00Z"},
        ]
    }

    assert plaid_link.finished_session(response)["link_session_id"] == "c"


def test_finished_session_none_while_in_progress():
    assert plaid_link.finished_session({"link_sessions": [{"started_at": "x"}]}) is None
    assert plaid_link.finished_session({}) is None


def test_item_add_results():
    session = {"results": {"item_add_results": [{"public_token": "p"}]}}

    assert plaid_link.item_add_results(session) == [{"public_token": "p"}]
    assert plaid_link.item_add_results({}) == []


def test_describe_exit_with_error():
    session = {
        "exit": {
            "error": {"error_code": "INVALID_CREDENTIALS", "display_message": "Wrong password"}
        }
    }

    assert plaid_link.describe_exit(session) == "INVALID_CREDENTIALS: Wrong password"


def test_describe_exit_without_error():
    session = {"exit": {"metadata": {"status": "institution_not_found"}}}

    assert plaid_link.describe_exit(session) == "exited without connecting (institution_not_found)"

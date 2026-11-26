"""Connections: saved platform accounts, their keys (by variable name) and the project choice."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.modules.accounts.access import required_role
from src.modules.connections.store import (
    ConnectionStore,
    SavedConnectionError,
    bound_key,
    read_env_file,
    secret_value,
)


def _store(tmp_path: Path, env: dict[str, str] | None = None) -> ConnectionStore:
    values = env or {}
    return ConnectionStore(tmp_path, secret_lookup=lambda name: values.get(name) or None)


def test_secret_value_reads_environment_and_rejects_bad_names() -> None:
    assert secret_value("SEMRUSH_AGENCY", environ={"SEMRUSH_AGENCY": "k1"}) == "k1"
    assert secret_value("SEMRUSH_AGENCY", environ={}) is None
    # Only upper-case variable names are read, so a connection cannot point at arbitrary settings.
    assert secret_value("cp_database_url", environ={"cp_database_url": "x"}) is None
    assert secret_value("PATH", environ={"PATH": "x"}) == "x"


def test_read_env_file_handles_comments_quotes_and_blanks(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nSEMRUSH_AGENCY=\"abc123\"\nAHREFS_KEY='def'\nBROKEN LINE\n",
        encoding="utf-8",
    )
    assert read_env_file(env) == {"SEMRUSH_AGENCY": "abc123", "AHREFS_KEY": "def"}
    assert read_env_file(tmp_path / "missing.env") == {}


def test_add_saves_a_connection_without_its_key(tmp_path: Path) -> None:
    store = _store(tmp_path, {"SEMRUSH_AGENCY": "secret-value"})
    item = store.add(
        platform="semrush", label="Agency login", secret_env="SEMRUSH_AGENCY", created_by="ana"
    )
    assert item.connection_id == "agency-login"
    raw = (tmp_path / "connections.json").read_text(encoding="utf-8")
    assert "SEMRUSH_AGENCY" in raw
    assert "secret-value" not in raw  # the key itself is never written to the app's data
    assert store.key_set(item) is True
    assert store.secret(item) == "secret-value"


def test_ids_stay_unique_for_the_same_label(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = store.add(platform="semrush", label="Agency", secret_env="SEMRUSH_A1", created_by="x")
    b = store.add(platform="semrush", label="Agency", secret_env="SEMRUSH_B1", created_by="x")
    assert (a.connection_id, b.connection_id) == ("agency", "agency-2")


@pytest.mark.parametrize(
    ("platform", "secret_env", "message"),
    [
        ("bing", "BING_KEY", "platform must be one of"),
        ("semrush", "semrush_key", "upper-case letters"),
        ("semrush", "X", "upper-case letters"),
        ("semrush", "SEMRUSH KEY", "upper-case letters"),
    ],
)
def test_malformed_connections_are_refused(
    tmp_path: Path, platform: str, secret_env: str, message: str
) -> None:
    with pytest.raises(SavedConnectionError, match=message):
        _store(tmp_path).add(platform=platform, label="L", secret_env=secret_env, created_by="x")


def test_the_same_key_cannot_be_saved_twice_for_one_platform(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(platform="ga4", label="One", secret_env="GA4_SA_MAIN", created_by="x")
    with pytest.raises(SavedConnectionError, match="already saved"):
        store.add(platform="ga4", label="Two", secret_env="GA4_SA_MAIN", created_by="x")
    # The same variable on another platform is a different connection and is allowed.
    store.add(platform="gsc", label="Search", secret_env="GA4_SA_MAIN", created_by="x")


def test_missing_key_is_reported_without_any_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.add(platform="ahrefs", label="Team", secret_env="AHREFS_TEAM", created_by="x")
    assert store.key_set(item) is False
    with pytest.raises(SavedConnectionError, match="set AHREFS_TEAM"):
        store.secret(item)


def test_remove_is_refused_while_a_project_uses_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.add(
        platform="semrush", label="Shared", secret_env="SEMRUSH_SHARED", created_by="x"
    )
    with pytest.raises(SavedConnectionError, match="still used by Client A"):
        store.remove(item.connection_id, in_use_by=["Client A"])
    store.remove(item.connection_id, in_use_by=[])
    assert store.all() == []


def test_bound_key_is_none_when_nothing_chosen(tmp_path: Path) -> None:
    store = _store(tmp_path, {"SEMRUSH_AGENCY": "k"})
    assert bound_key(store, {}, "semrush") is None
    assert bound_key(store, {"ahrefs": "whatever"}, "semrush") is None


def test_bound_key_uses_the_chosen_connection(tmp_path: Path) -> None:
    store = _store(tmp_path, {"SEMRUSH_AGENCY": "agency-key"})
    item = store.add(
        platform="semrush", label="Agency", secret_env="SEMRUSH_AGENCY", created_by="x"
    )
    assert bound_key(store, {"semrush": item.connection_id}, "semrush") == "agency-key"


def test_bound_key_never_falls_back_to_another_key(tmp_path: Path) -> None:
    store = _store(tmp_path)  # the variable is not set
    item = store.add(
        platform="semrush", label="Agency", secret_env="SEMRUSH_AGENCY", created_by="x"
    )
    with pytest.raises(SavedConnectionError, match="has no key"):
        bound_key(store, {"semrush": item.connection_id}, "semrush")


def test_bound_key_refuses_a_connection_that_was_deleted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SavedConnectionError, match="there is no connection"):
        bound_key(store, {"semrush": "gone"}, "semrush")


# -- who may do what (the access gate) -------------------------------------------------


def test_listing_connections_is_viewer_work() -> None:
    assert required_role("GET", "/api/connections") == "viewer"


def test_saving_or_removing_a_connection_is_admin_work() -> None:
    assert required_role("POST", "/api/connections") == "admin"
    assert required_role("DELETE", "/api/connections/agency") == "admin"


def test_choosing_a_project_connection_is_editor_work() -> None:
    assert required_role("PUT", "/api/projects/p1/connections") == "editor"

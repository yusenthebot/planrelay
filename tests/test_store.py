from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from planrelay_bridge.store import RelayError, Store


def payload() -> dict[str, object]:
    files = [{"path": "app.py", "sha256": "b" * 64}]
    head = "a" * 40
    revision = hashlib.sha256(
        json.dumps(
            {"git_head": head, "files": files},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return {
        "project_id": "demo",
        "git_head": head,
        "files": files,
        "revision": revision,
        "context": "# Synthetic context\n",
    }


def setup_store(tmp_path: Path) -> tuple[Store, str, str]:
    store = Store(tmp_path / "relay.sqlite")
    pair = store.redeem_pairing(store.create_pairing("owner"))
    worker, token = pair["worker_id"], pair["token"]
    store.register_project("owner", worker, payload())
    return store, worker, token


def test_pairing_one_use_tokens_not_stored_in_plaintext(tmp_path: Path) -> None:
    store = Store(tmp_path / "relay.sqlite")
    code = store.create_pairing("owner")
    pair = store.redeem_pairing(code)
    assert store.worker_identity(pair["token"]) == ("owner", pair["worker_id"])
    with pytest.raises(RelayError):
        store.redeem_pairing(code)
    with pytest.raises(RelayError):
        store.worker_identity("wrong")
    raw = (tmp_path / "relay.sqlite").read_bytes()
    assert code.encode() not in raw
    assert pair["token"].encode() not in raw


def test_task_roundtrip_and_idempotency(tmp_path: Path) -> None:
    store, worker, _ = setup_store(tmp_path)
    revision = str(payload()["revision"])
    first = store.submit(
        "owner", "demo", "Fix inputs", "A proposal", revision, "request-1"
    )
    again = store.submit(
        "owner", "demo", "Fix inputs", "A proposal", revision, "request-1"
    )
    assert first["id"] == again["id"]
    assert "lease_token" not in first
    claimed = store.claim("owner", worker)
    assert claimed and claimed["id"] == first["id"]
    assert store.claim("owner", worker) is None
    lease = claimed["lease_token"]
    assert store.heartbeat("owner", worker, first["id"], lease)["state"] == "running"
    result = {"summary": "Implemented and tested", "changed_files": ["app.py"]}
    assert (
        store.finish("owner", worker, first["id"], lease, "succeeded", result)["result"]
        == result
    )
    assert (
        store.finish("owner", worker, first["id"], lease, "succeeded", result)["state"]
        == "succeeded"
    )
    with pytest.raises(RelayError):
        store.submit("owner", "demo", "Changed", "A proposal", revision, "request-1")


def test_owner_worker_and_lease_isolation(tmp_path: Path) -> None:
    store, worker, token = setup_store(tmp_path)
    job = store.submit(
        "owner", "demo", "Fix", "Plan", str(payload()["revision"]), "request-1"
    )
    for operation in (
        lambda: store.get_job("stranger", job["id"]),
        lambda: store.get_project("stranger", "demo"),
        lambda: store.register_project("stranger", worker, payload()),
    ):
        with pytest.raises(RelayError):
            operation()
    claimed = store.claim("owner", worker)
    assert claimed
    with pytest.raises(RelayError):
        store.finish("owner", worker, job["id"], "wrong", "succeeded", {})
    assert store.worker_identity(token)[0] == "owner"


def test_cancel_and_expired_lease_never_automatically_reexecute(tmp_path: Path) -> None:
    now = [1000.0]
    store = Store(tmp_path / "relay.sqlite", clock=lambda: now[0])
    pair = store.redeem_pairing(store.create_pairing("owner"))
    worker = pair["worker_id"]
    store.register_project("owner", worker, payload())
    one = store.submit(
        "owner", "demo", "Fix", "Plan", str(payload()["revision"]), "one"
    )
    store.cancel("owner", one["id"])
    assert store.claim("owner", worker) is None
    two = store.submit(
        "owner", "demo", "Fix", "Plan", str(payload()["revision"]), "two"
    )
    claimed = store.claim("owner", worker, lease_seconds=60)
    assert claimed
    now[0] += 61
    assert store.claim("owner", worker) is None
    assert store.get_job("owner", two["id"])["state"] == "blocked"
    with pytest.raises(RelayError):
        store.finish(
            "owner", worker, two["id"], claimed["lease_token"], "succeeded", {}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", "../evil"),
        ("revision", "wrong"),
        ("context", "sk-abcdefgh12345678"),
        ("git_head", None),
        ("files", [{"path": ".env", "sha256": "b" * 64}]),
    ],
)
def test_invalid_project_data_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    store = Store(tmp_path / "relay.sqlite")
    pair = store.redeem_pairing(store.create_pairing("owner"))
    data = payload()
    data[field] = value
    with pytest.raises(RelayError):
        store.register_project("owner", pair["worker_id"], data)


def test_stale_revision_and_queue_limit(tmp_path: Path) -> None:
    store, _, _ = setup_store(tmp_path)
    with pytest.raises(RelayError):
        store.submit("owner", "demo", "Fix", "Plan", "c" * 64, "one")
    for index in range(32):
        store.submit(
            "owner", "demo", "Fix", "Plan", str(payload()["revision"]), f"job-{index}"
        )
    with pytest.raises(RelayError):
        store.submit(
            "owner", "demo", "Fix", "Plan", str(payload()["revision"]), "overflow"
        )


def test_revoke_device_stops_authentication_and_running_lease(tmp_path: Path) -> None:
    store, worker, token = setup_store(tmp_path)
    job = store.submit("owner", "demo", "Fix", "Plan", str(payload()["revision"]), "x")
    claimed = store.claim("owner", worker)
    assert claimed
    with pytest.raises(RelayError):
        store.revoke_worker("stranger", worker)
    store.revoke_worker("owner", worker)
    with pytest.raises(RelayError):
        store.worker_identity(token)
    with pytest.raises(RelayError):
        store.heartbeat("owner", worker, job["id"], claimed["lease_token"])
    assert store.get_job("owner", job["id"])["state"] == "blocked"


def test_worker_read_and_claim_are_scoped_to_bound_project(tmp_path: Path) -> None:
    store, worker, _ = setup_store(tmp_path)
    other = store.redeem_pairing(store.create_pairing("owner"))["worker_id"]
    data = payload()
    data["project_id"] = "other"
    store.register_project("owner", other, data)
    job = store.submit("owner", "other", "Fix", "Plan", str(data["revision"]), "other")
    assert store.claim("owner", worker, project_id="other") is None
    assert (
        store.relay_read("owner", worker, "list_projects")
        == store.list_projects("owner")[:1]
    )
    assert (
        store.relay_read("owner", worker, "get_project_context", "demo")["project_id"]
        == "demo"
    )
    for action, project, run in [
        ("get_project_context", "other", None),
        ("get_run", None, job["id"]),
    ]:
        with pytest.raises(RelayError):
            store.relay_read("owner", worker, action, project, run)


def test_concurrent_devices_cannot_overwrite_project_binding(tmp_path: Path) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    store = Store(tmp_path / "relay.sqlite")
    workers = [
        store.redeem_pairing(store.create_pairing("owner"))["worker_id"]
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)

    def register(worker: str) -> str | None:
        data = payload()
        data["context"] = worker
        barrier.wait(timeout=5)
        try:
            store.register_project("owner", worker, data)
            return worker
        except RelayError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(register, workers))
    accepted = [item for item in results if item is not None]
    assert len(accepted) == 1
    assert store.get_project("owner", "demo")["context"] == accepted[0]


def test_nonfinite_json_cannot_commit_terminal_state(tmp_path: Path) -> None:
    store, worker, _ = setup_store(tmp_path)
    job = store.submit(
        "owner", "demo", "Fix", "Plan", str(payload()["revision"]), "finite"
    )
    claimed = store.claim("owner", worker)
    assert claimed
    for value in (float("nan"), float("inf")):
        with pytest.raises(RelayError):
            store.finish(
                "owner",
                worker,
                job["id"],
                claimed["lease_token"],
                "succeeded",
                {"metric": value},
            )
        assert store.get_job("owner", job["id"])["state"] == "running"


def test_owner_explicitly_recovers_revoked_project_binding(tmp_path: Path) -> None:
    store, old, _ = setup_store(tmp_path)
    store.revoke_worker("owner", old)
    device = store.redeem_pairing(store.create_pairing("owner"))["worker_id"]
    with pytest.raises(RelayError):
        store.register_project("owner", device, payload())
    with pytest.raises(RelayError):
        store.rebind_project("stranger", "demo", device)
    store.rebind_project("owner", "demo", device)
    with pytest.raises(RelayError):
        store.submit("owner", "demo", "Fix", "Plan", str(payload()["revision"]), "old")
    assert store.get_project("owner", "demo")["context"] == ""
    store.register_project("owner", device, payload())

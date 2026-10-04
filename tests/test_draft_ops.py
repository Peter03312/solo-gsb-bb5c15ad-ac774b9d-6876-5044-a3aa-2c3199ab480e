"""按键合并草稿（PATCH /draft）：多人协作的增量编辑。

一组 set/delete 操作 + 所见草稿修订号；每次草稿变更（含整体替换）都留下
可核对的修订快照。所见修订落后时，仅当本次涉及的每个键相对该快照均未改变
才合并（新修订 +1）；否则返回冲突键与当前修订号（409），草稿不变。
重复操作键、删除不存在的规则、无效规则整组拒绝（422）；
无法取得所见修订快照时明确拒绝（409）。并发提交原子核对与写入。
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM draft_revision_rules;"
        "DELETE FROM draft_revisions;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    db.init_db()  # 重建当前修订（0）的空草稿快照
    with TestClient(app) as c:
        yield c


RULES_V1 = [
    {
        "match_type": "exact",
        "match_key": "alice@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "alice@mail.example.com"}],
    },
    {
        "match_type": "exact",
        "match_key": "bob@corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "bob@mail.example.com"}],
    },
    {
        "match_type": "domain",
        "match_key": "corp.example.com",
        "action": "route",
        "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}],
    },
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def patch_ops(client, revision, ops):
    return client.patch("/draft", json={"revision": revision, "ops": ops})


def set_op(key, targets, match_type="exact", action="route"):
    return {"op": "set", "match_type": match_type, "match_key": key, "action": action, "targets": targets}


def delete_op(key, match_type="exact"):
    return {"op": "delete", "match_type": match_type, "match_key": key}


def draft_keys(client):
    draft = client.get("/draft").json()
    return {(r["match_type"], r["match_key"]) for r in draft["rules"]}, draft["revision"]


def setup_draft(client):
    assert put_rules(client, 0, RULES_V1).status_code == 200
    return 1  # 当前修订号


# ---------- 基本合并：所见即当前 ----------

def test_patch_set_and_delete_on_current_revision(client):
    revision = setup_draft(client)
    resp = patch_ops(client, revision, [
        set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}]),
        delete_op("bob@corp.example.com"),
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    keys, current = draft_keys(client)
    assert current == 2
    assert keys == {
        ("exact", "alice@corp.example.com"),
        ("exact", "carol@corp.example.com"),
        ("domain", "corp.example.com"),
    }
    # 响应即合并后的完整草稿，与 GET /draft 一致
    assert resp.json()["rules"] == client.get("/draft").json()["rules"]


def test_patch_set_overwrites_existing_rule(client):
    revision = setup_draft(client)
    resp = patch_ops(client, revision, [
        set_op("alice@corp.example.com", [{"kind": "mailbox", "value": "alice-new@mail.example.com"}]),
    ])
    assert resp.status_code == 200
    rules = {r["match_key"]: r for r in resp.json()["rules"]}
    assert rules["alice@corp.example.com"]["targets"] == [
        {"kind": "mailbox", "value": "alice-new@mail.example.com"}
    ]


def test_patch_normalizes_keys_and_targets(client):
    revision = setup_draft(client)
    resp = patch_ops(client, revision, [
        set_op("  CAROL@Corp.Example.COM ", [{"kind": "mailbox", "value": " Carol@Mail.Example.Com "}]),
        delete_op(" BOB@corp.example.com"),
    ])
    assert resp.status_code == 200
    keys, _ = draft_keys(client)
    assert ("exact", "carol@corp.example.com") in keys
    assert ("exact", "bob@corp.example.com") not in keys
    rules = {r["match_key"]: r for r in client.get("/draft").json()["rules"]}
    assert rules["carol@corp.example.com"]["targets"] == [
        {"kind": "mailbox", "value": "carol@mail.example.com"}
    ]


# ---------- 落后修订：键未变则合并，键已变则冲突 ----------

def test_stale_revision_merges_when_touched_keys_unchanged(client):
    revision = setup_draft(client)  # rev 1
    # 另一维护者整体替换：改 alice、保留 bob 与域兜底
    rules_v2 = [
        {"match_type": "exact", "match_key": "alice@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "alice-v2@mail.example.com"}]},
        {"match_type": "exact", "match_key": "bob@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "bob@mail.example.com"}]},
        {"match_type": "domain", "match_key": "corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "catchall@mail.example.com"}]},
    ]
    assert put_rules(client, 1, rules_v2).status_code == 200  # rev 2
    # 基于所见 rev 1 提交：只动 bob（相对快照未变）→ 合并成功
    resp = patch_ops(client, revision, [
        set_op("bob@corp.example.com", [{"kind": "mailbox", "value": "bob-new@mail.example.com"}]),
    ])
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 3
    rules = {r["match_key"]: r for r in resp.json()["rules"]}
    # 双方修改都在：alice 是 rev 2 的值，bob 是本次合并的值
    assert rules["alice@corp.example.com"]["targets"] == [{"kind": "mailbox", "value": "alice-v2@mail.example.com"}]
    assert rules["bob@corp.example.com"]["targets"] == [{"kind": "mailbox", "value": "bob-new@mail.example.com"}]


def test_stale_revision_conflict_returns_conflicting_keys(client):
    revision = setup_draft(client)  # rev 1
    # 另一维护者改掉 alice 并删掉域兜底
    rules_v2 = [
        {"match_type": "exact", "match_key": "alice@corp.example.com", "action": "reject"},
        {"match_type": "exact", "match_key": "bob@corp.example.com", "action": "route",
         "targets": [{"kind": "mailbox", "value": "bob@mail.example.com"}]},
    ]
    assert put_rules(client, 1, rules_v2).status_code == 200  # rev 2
    before = client.get("/draft").json()
    # 基于 rev 1：set alice（已变）+ delete 域兜底（已消失）+ set bob（未变）
    resp = patch_ops(client, revision, [
        set_op("alice@corp.example.com", [{"kind": "mailbox", "value": "x@mail.example.com"}]),
        delete_op("corp.example.com", match_type="domain"),
        set_op("bob@corp.example.com", [{"kind": "mailbox", "value": "bob-new@mail.example.com"}]),
    ])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "keys_conflict"
    assert detail["current_revision"] == 2
    assert {tuple(k.values()) for k in detail["conflicting_keys"]} == {
        ("exact", "alice@corp.example.com"),
        ("domain", "corp.example.com"),
    }  # bob 未变，不在冲突键中
    assert client.get("/draft").json() == before  # 草稿不变


def test_conflict_when_key_deleted_by_other_maintainer(client):
    revision = setup_draft(client)
    assert put_rules(client, 1, RULES_V1[:2]).status_code == 200  # rev 2：删掉域兜底
    resp = patch_ops(client, revision, [
        set_op("corp.example.com", [{"kind": "mailbox", "value": "new@mail.example.com"}], match_type="domain"),
    ])
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicting_keys"] == [
        {"match_type": "domain", "match_key": "corp.example.com"}
    ]


def test_snapshot_covers_full_replacement(client):
    # 整体替换留下的快照同样可用于核对：未动的键可基于旧修订合并
    revision = setup_draft(client)  # rev 1
    rules_v2 = [dict(r) for r in RULES_V1]  # 内容完全相同的整体替换
    assert put_rules(client, 1, rules_v2).status_code == 200  # rev 2
    resp = patch_ops(client, revision, [
        set_op("dave@corp.example.com", [{"kind": "mailbox", "value": "dave@mail.example.com"}]),
    ])
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3
    keys, _ = draft_keys(client)
    assert ("exact", "dave@corp.example.com") in keys


def test_snapshot_unavailable_when_revision_out_of_range(client):
    setup_draft(client)  # 当前 rev 1
    resp = patch_ops(client, 7, [  # 所见修订从未存在
        set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}]),
    ])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "snapshot_unavailable"
    assert detail["current_revision"] == 1
    assert detail["seen_revision"] == 7
    keys, current = draft_keys(client)
    assert current == 1  # 草稿不变


# ---------- 整组拒绝：重复键 / 删除不存在 / 无效规则 ----------

def test_duplicate_op_keys_rejected(client):
    revision = setup_draft(client)
    resp = patch_ops(client, revision, [
        set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "a@mail.example.com"}]),
        delete_op(" CAROL@corp.example.com"),  # 规范化后同一键
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_ops"
    assert "duplicate" in resp.json()["detail"]["message"]
    _, current = draft_keys(client)
    assert current == 1  # 整组拒绝，草稿不变


def test_delete_missing_rule_rejected(client):
    revision = setup_draft(client)
    before = client.get("/draft").json()
    resp = patch_ops(client, revision, [
        set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}]),
        delete_op("ghost@corp.example.com"),  # 当前草稿不存在
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "delete_missing"
    assert detail["missing_keys"] == [{"match_type": "exact", "match_key": "ghost@corp.example.com"}]
    assert client.get("/draft").json() == before  # 整组拒绝（set 也不生效）


def test_delete_missing_checked_against_current_draft(client):
    # 落后修订下删除：键相对快照未变（快照里也不存在）→ delete_missing 而非冲突
    revision = setup_draft(client)
    assert put_rules(client, 1, RULES_V1[:2]).status_code == 200  # rev 2：删掉域兜底
    resp = patch_ops(client, revision, [delete_op("ghost@corp.example.com")])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "delete_missing"


def test_invalid_rule_rejected(client):
    revision = setup_draft(client)
    bad_ops = [
        [set_op("not-an-address", [{"kind": "mailbox", "value": "a@mail.example.com"}])],  # 无效键
        [set_op("a@corp.example.com", [])],  # route 无目标
        [set_op("a@corp.example.com", [{"kind": "mailbox", "value": "a@mail.example.com"}], action="reject")],  # reject 带目标
        [set_op("a@corp.example.com", [{"kind": "mailbox", "value": "broken"}])],  # 无效目标
        [set_op("corp@example.com", [{"kind": "mailbox", "value": "a@mail.example.com"}], match_type="domain")],  # 无效域
    ]
    for ops in bad_ops:
        resp = patch_ops(client, revision, ops)
        assert resp.status_code == 422, ops
        assert resp.json()["detail"]["error"] == "invalid_ops"
    _, current = draft_keys(client)
    assert current == 1  # 全部整组拒绝


def test_malformed_ops_rejected(client):
    revision = setup_draft(client)
    # delete 携带内容
    resp = patch_ops(client, revision, [
        {"op": "delete", "match_type": "exact", "match_key": "bob@corp.example.com", "action": "reject"}
    ])
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "invalid_ops"
    # set 缺 action
    resp = patch_ops(client, revision, [
        {"op": "set", "match_type": "exact", "match_key": "carol@corp.example.com",
         "targets": [{"kind": "mailbox", "value": "carol@mail.example.com"}]}
    ])
    assert resp.status_code == 422
    # 空操作组
    assert patch_ops(client, revision, []).status_code == 422
    _, current = draft_keys(client)
    assert current == 1


# ---------- 与既有流程的衔接 ----------

def test_merged_draft_publishes_through_original_flow(client):
    revision = setup_draft(client)  # rev 1
    resp = patch_ops(client, revision, [
        set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}]),
        delete_op("bob@corp.example.com"),
    ])
    assert resp.status_code == 200
    new_revision = resp.json()["revision"]
    # 按原流程发布合并后的草稿
    pub = client.post("/publish", json={"revision": new_revision})
    assert pub.status_code == 200, pub.text
    version = pub.json()["version"]
    resolved = client.post("/resolve", json={"address": "carol@corp.example.com", "version": version})
    assert resolved.status_code == 200
    assert [e["mailbox"] for e in resolved.json()["endpoints"]] == ["carol@mail.example.com"]
    # 既有接口契约不变：整体替换仍带修订号、冲突仍 409
    assert put_rules(client, new_revision, RULES_V1).status_code == 200
    assert put_rules(client, new_revision, RULES_V1).status_code == 409


def test_put_draft_conflict_contract_unchanged(client):
    revision = setup_draft(client)
    resp = put_rules(client, revision + 5, RULES_V1)
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "revision_conflict"


# ---------- 并发：原子核对与写入 ----------

def test_concurrent_disjoint_ops_both_merge(client):
    revision = setup_draft(client)  # 双方基于同一所见修订 rev 1
    ops_a = [set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}])]
    ops_b = [delete_op("bob@corp.example.com")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda ops: patch_ops(client, revision, ops), [ops_a, ops_b]))
    assert sorted(r.status_code for r in responses) == [200, 200]
    keys, current = draft_keys(client)
    assert current == 3  # 两次合并各产生一个新修订
    assert ("exact", "carol@corp.example.com") in keys
    assert ("exact", "bob@corp.example.com") not in keys


def test_concurrent_same_key_exactly_one_wins(client):
    revision = setup_draft(client)
    ops_a = [set_op("alice@corp.example.com", [{"kind": "mailbox", "value": "a1@mail.example.com"}])]
    ops_b = [set_op("alice@corp.example.com", [{"kind": "mailbox", "value": "a2@mail.example.com"}])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda ops: patch_ops(client, revision, ops), [ops_a, ops_b]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["detail"]["error"] == "keys_conflict"
    assert loser.json()["detail"]["conflicting_keys"] == [
        {"match_type": "exact", "match_key": "alice@corp.example.com"}
    ]
    # 最终草稿恰为胜出者的值，且只前进一个修订
    rules = {r["match_key"]: r for r in client.get("/draft").json()["rules"]}
    winner_target = next(
        ops[0]["targets"] for r, ops in zip(responses, [ops_a, ops_b]) if r.status_code == 200
    )
    assert rules["alice@corp.example.com"]["targets"] == winner_target
    _, current = draft_keys(client)
    assert current == 2


def test_concurrent_patch_and_put_serialized(client):
    revision = setup_draft(client)
    ops = [set_op("carol@corp.example.com", [{"kind": "mailbox", "value": "carol@mail.example.com"}])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(patch_ops, client, revision, ops),
            pool.submit(put_rules, client, revision, RULES_V1[:2]),
        ]
        patch_resp, put_resp = (f.result() for f in futures)
    # PATCH 的键不被整体替换触及，无论先后顺序都能合并
    assert patch_resp.status_code == 200
    # 整体替换要求所见即当前：先到者成功，后到者 409
    assert put_resp.status_code in (200, 409)
    _, current = draft_keys(client)
    assert current == (3 if put_resp.status_code == 200 else 2)

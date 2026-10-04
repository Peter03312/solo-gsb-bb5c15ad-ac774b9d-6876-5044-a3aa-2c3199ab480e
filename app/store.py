"""持久层：草稿（带乐观锁修订号）、草稿修订快照（按键合并的核对基准）、不可变发布版本、默认投递策略（基线/候选灰度）与幂等投递判定。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .routing import Rule, RuleSet, Target


class RevisionConflict(Exception):
    """所见修订号与当前修订号不一致（草稿或默认投递策略）。"""

    def __init__(self, current: int, subject: str = "draft"):
        super().__init__(f"revision conflict: current {subject} revision is {current}")
        self.current = current


class KeysConflict(Exception):
    """所见修订落后时，本次操作涉及的键相对所见快照已发生改变。"""

    def __init__(self, current: int, conflicting_keys: list[tuple[str, str]]):
        super().__init__(f"keys changed since seen revision: {conflicting_keys}")
        self.current = current
        self.conflicting_keys = conflicting_keys


class SnapshotUnavailable(Exception):
    """无法取得所见修订的草稿快照（修订号超出范围或快照缺失），明确拒绝合并。"""

    def __init__(self, current: int, seen: int):
        super().__init__(f"no draft snapshot for seen revision {seen} (current revision {current})")
        self.current = current
        self.seen = seen


class DeleteMissingRule(Exception):
    """删除当前草稿中不存在的规则键。"""

    def __init__(self, missing_keys: list[tuple[str, str]]):
        super().__init__(f"cannot delete rules absent from current draft: {missing_keys}")
        self.missing_keys = missing_keys


class PolicyVersionNotFound(Exception):
    """策略引用了不存在的已发布版本。"""

    def __init__(self, version: int):
        super().__init__(f"published version not found: {version}")
        self.version = version


def _row_to_rule(row: sqlite3.Row) -> Rule:
    targets = tuple(Target(kind=t["kind"], value=t["value"]) for t in json.loads(row["targets"]))
    return Rule(
        match_type=row["match_type"],
        match_key=row["match_key"],
        action=row["action"],
        targets=targets,
    )


def rule_to_dict(rule: Rule) -> dict:
    return {
        "match_type": rule.match_type,
        "match_key": rule.match_key,
        "action": rule.action,
        "targets": [{"kind": t.kind, "value": t.value} for t in rule.targets],
    }


def get_draft(conn: sqlite3.Connection) -> tuple[int, list[Rule]]:
    revision = conn.execute("SELECT revision FROM draft_meta WHERE id = 1").fetchone()["revision"]
    rows = conn.execute(
        "SELECT match_type, match_key, action, targets FROM draft_rules ORDER BY match_type, match_key"
    ).fetchall()
    return revision, [_row_to_rule(r) for r in rows]


def _insert_rules(
    conn: sqlite3.Connection,
    table: str,
    rules: list[Rule],
    prefix: tuple = (),
    prefix_column: str = "revision",
) -> None:
    """把规则写入指定规则表（draft_rules / draft_revision_rules / version_rules 同构）。"""
    lead = f"{prefix_column}, " if prefix else ""
    conn.executemany(
        f"INSERT INTO {table} ({lead}match_type, match_key, action, targets) "
        f"VALUES ({'?, ' if prefix else ''}?, ?, ?, ?)",
        [
            (
                *prefix,
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )


def replace_draft(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """整体替换草稿；修订号不匹配抛 RevisionConflict。须在事务内调用。

    成功后为新修订留下完整快照，供后续按键合并核对。
    """
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    conn.execute("DELETE FROM draft_rules")
    _insert_rules(conn, "draft_rules", rules)
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    save_draft_snapshot(conn, new_revision, rules)
    return new_revision


# ---------- 草稿修订快照（每次变更留底，供落后修订的按键合并核对） ----------

def save_draft_snapshot(conn: sqlite3.Connection, revision: int, rules: list[Rule]) -> None:
    """为某个草稿修订保存完整规则快照。快照不可变，须在事务内随变更一起写入。"""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute("INSERT INTO draft_revisions (revision, created_at) VALUES (?, ?)", (revision, created_at))
    _insert_rules(conn, "draft_revision_rules", rules, prefix=(revision,))


def get_draft_snapshot(conn: sqlite3.Connection, revision: int) -> list[Rule] | None:
    """读取某个修订的草稿快照；该修订无快照（超出范围或缺失）返回 None。"""
    row = conn.execute("SELECT revision FROM draft_revisions WHERE revision = ?", (revision,)).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        "SELECT match_type, match_key, action, targets FROM draft_revision_rules "
        "WHERE revision = ? ORDER BY match_type, match_key",
        (revision,),
    ).fetchall()
    return [_row_to_rule(r) for r in rows]


def _rules_map(rules: list[Rule]) -> dict[tuple[str, str], Rule]:
    return {(r.match_type, r.match_key): r for r in rules}


def apply_draft_ops(
    conn: sqlite3.Connection,
    seen_revision: int,
    set_rules: dict[tuple[str, str], Rule],
    delete_keys: list[tuple[str, str]],
) -> tuple[int, list[Rule]]:
    """把一组按键操作（set/delete）合并到当前草稿，返回 (新修订号, 合并后完整规则)。

    须在事务内调用，核对与写入随事务原子完成：
    - 所见修订即当前修订：直接合并；
    - 所见修订落后：取出该修订的草稿快照，仅当本次涉及的每个键相对快照均未改变
      （内容相同、或同样不存在）才合并，否则抛 KeysConflict（草稿不变）；
      快照缺失抛 SnapshotUnavailable；
    - 删除当前草稿不存在的键抛 DeleteMissingRule（整组回滚）；
    - 合并成功产生新修订并留下新修订快照。
    """
    current_revision, current_rules = get_draft(conn)
    current_map = _rules_map(current_rules)
    touched = list(set_rules) + delete_keys  # set/delete 键已在规范化阶段去重，互不重叠

    if seen_revision != current_revision:
        snapshot = get_draft_snapshot(conn, seen_revision)
        if snapshot is None:
            raise SnapshotUnavailable(current_revision, seen_revision)
        snapshot_map = _rules_map(snapshot)
        conflicting = [key for key in touched if snapshot_map.get(key) != current_map.get(key)]
        if conflicting:
            raise KeysConflict(current_revision, conflicting)

    missing = [key for key in delete_keys if key not in current_map]
    if missing:
        raise DeleteMissingRule(missing)

    merged = dict(current_map)
    for key in delete_keys:
        del merged[key]
    merged.update(set_rules)
    merged_rules = [merged[key] for key in sorted(merged)]

    conn.execute("DELETE FROM draft_rules")
    _insert_rules(conn, "draft_rules", merged_rules)
    new_revision = current_revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    save_draft_snapshot(conn, new_revision, merged_rules)
    return new_revision, merged_rules


def publish(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """把当前草稿固化为不可变版本，返回版本号。须在事务内调用。"""
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO versions (source_revision, created_at) VALUES (?, ?)",
        (revision, created_at),
    )
    version = cur.lastrowid
    _insert_rules(conn, "version_rules", rules, prefix=(version,), prefix_column="version")
    return version


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.version, v.source_revision, v.created_at, COUNT(r.match_key) AS rule_count
        FROM versions v LEFT JOIN version_rules r ON r.version = v.version
        GROUP BY v.version ORDER BY v.version
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version: int) -> dict | None:
    row = conn.execute(
        "SELECT version, source_revision, created_at FROM versions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        return None
    rules = conn.execute(
        "SELECT match_type, match_key, action, targets FROM version_rules "
        "WHERE version = ? ORDER BY match_type, match_key",
        (version,),
    ).fetchall()
    return {**dict(row), "rules": [_row_to_rule(r) for r in rules]}


def load_ruleset(conn: sqlite3.Connection, version: int) -> RuleSet | None:
    found = get_version(conn, version)
    if found is None:
        return None
    return RuleSet(found["rules"])


# ---------- 默认投递策略（基线 / 候选灰度） ----------

def get_policy(conn: sqlite3.Connection) -> dict | None:
    """读取当前默认投递策略；从未配置时返回 None（修订号视为 0）。"""
    row = conn.execute(
        "SELECT revision, baseline_version, candidate_version, candidate_percent, updated_at "
        "FROM delivery_policy WHERE id = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def replace_policy(
    conn: sqlite3.Connection,
    expected_revision: int,
    baseline_version: int,
    candidate_version: int,
    candidate_percent: int,
) -> dict:
    """在同一事务内核验所见策略修订号并保存新策略。

    - 未配置策略时修订号为 0，行尚不存在；
    - 修订号不匹配抛 RevisionConflict，调用方回滚，原策略保持不变；
    - 基线 / 候选必须都是已发布版本，否则抛 PolicyVersionNotFound（同样不改原策略）。
    """
    current = get_policy(conn)
    current_revision = current["revision"] if current is not None else 0
    if current_revision != expected_revision:
        raise RevisionConflict(current_revision, subject="policy")
    for version in (baseline_version, candidate_version):
        if conn.execute("SELECT 1 FROM versions WHERE version = ?", (version,)).fetchone() is None:
            raise PolicyVersionNotFound(version)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_policy (id, revision, baseline_version, candidate_version, candidate_percent, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            baseline_version = excluded.baseline_version,
            candidate_version = excluded.candidate_version,
            candidate_percent = excluded.candidate_percent,
            updated_at = excluded.updated_at
        """,
        (current_revision + 1, baseline_version, candidate_version, candidate_percent, updated_at),
    )
    return {
        "revision": current_revision + 1,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "candidate_percent": candidate_percent,
        "updated_at": updated_at,
    }


# ---------- 幂等投递判定（投递编号 → 首次判定，永久可查、原样重放） ----------

def get_decision(conn: sqlite3.Connection, delivery_id: str) -> dict | None:
    """按投递编号读取已持久化的判定；不存在返回 None。JSON 字段在此还原。"""
    row = conn.execute(
        "SELECT delivery_id, address, policy_revision, baseline_version, candidate_version, "
        "candidate_percent, selection, version, status, endpoints, error, created_at "
        "FROM delivery_decisions WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    if row is None:
        return None
    decision = dict(row)
    decision["endpoints"] = json.loads(decision["endpoints"]) if decision["endpoints"] is not None else None
    decision["error"] = json.loads(decision["error"]) if decision["error"] is not None else None
    return decision


def save_decision(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    address: str,
    policy: dict,
    selection: str,
    version: int,
    status: str,
    endpoints: list[dict] | None,
    error: dict | None,
) -> dict:
    """把首次判定整体持久化（成功含全部终点路径，失败含原错误详情）。须在事务内调用。

    判定一旦写入不再改写：策略后续变更不影响已持久化的结果。
    """
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_decisions (
            delivery_id, address, policy_revision, baseline_version, candidate_version,
            candidate_percent, selection, version, status, endpoints, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            address,
            policy["revision"],
            policy["baseline_version"],
            policy["candidate_version"],
            policy["candidate_percent"],
            selection,
            version,
            status,
            json.dumps(endpoints) if endpoints is not None else None,
            json.dumps(error) if error is not None else None,
            created_at,
        ),
    )
    return get_decision(conn, delivery_id)

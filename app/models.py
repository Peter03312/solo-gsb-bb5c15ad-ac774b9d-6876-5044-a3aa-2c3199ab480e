"""请求模型与草稿规则的编辑期校验（无效地址 / 重复规则 / 空目标在此拒绝）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .routing import Rule, Target, is_valid_address, is_valid_domain


class TargetIn(BaseModel):
    kind: Literal["alias", "mailbox"]
    value: str


class RuleIn(BaseModel):
    match_type: Literal["exact", "domain"]
    match_key: str
    action: Literal["reject", "route"]
    targets: list[TargetIn] = Field(default_factory=list)


class DraftUpdate(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")
    rules: list[RuleIn]


class DraftOp(BaseModel):
    """单个按键操作：set 设置（或覆盖）一条规则；delete 删除一条规则。"""

    op: Literal["set", "delete"]
    match_type: Literal["exact", "domain"]
    match_key: str
    action: Literal["reject", "route"] | None = None
    targets: list[TargetIn] = Field(default_factory=list)


class DraftOpsUpdate(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")
    ops: list[DraftOp] = Field(min_length=1, description="一组按键 set/delete 操作（至少一条）")


class PublishRequest(BaseModel):
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class ResolveRequest(BaseModel):
    address: str
    version: int = Field(ge=1, description="已发布版本号")


class PreviewRequest(BaseModel):
    base_version: int = Field(ge=1, description="基线已发布版本号")
    revision: int = Field(ge=0, description="客户端所见草稿修订号")


class PolicyUpdate(BaseModel):
    baseline_version: int = Field(ge=1, description="基线已发布版本号")
    candidate_version: int = Field(ge=1, description="候选已发布版本号")
    candidate_percent: int = Field(ge=0, le=100, description="候选流量比例（整数 0–100）")
    revision: int = Field(ge=0, description="客户端所见策略修订号（未配置时为 0）")


class DeliverRequest(BaseModel):
    address: str


class DeliverySubmit(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=200, description="调用方唯一投递编号")
    address: str


class ValidationFailure(Exception):
    """编辑期校验失败，message 直接作为 4xx 响应。"""


def _normalize_key(match_type: str, raw_key: str) -> str:
    """规则键规范化（小写、去空白）并校验：exact 为完整地址，domain 为域。"""
    key = raw_key.strip().lower()
    if match_type == "exact":
        if not is_valid_address(key):
            raise ValidationFailure(f"invalid address in exact rule: {raw_key!r}")
    else:
        if not is_valid_domain(key):
            raise ValidationFailure(f"invalid domain in domain rule: {raw_key!r}")
    return key


def _normalize_rule(match_type: str, key: str, action: str, raw_targets: list[TargetIn]) -> Rule:
    """单条规则内容校验：route 至少一个目标、reject 不可带目标、目标地址有效且去重。"""
    if action == "reject":
        if raw_targets:
            raise ValidationFailure(f"reject rule for {key!r} must not have targets")
        targets: tuple[Target, ...] = ()
    else:
        if not raw_targets:
            raise ValidationFailure(f"route rule for {key!r} has empty targets")
        normalized: list[Target] = []
        seen_targets: set[tuple[str, str]] = set()
        for t in raw_targets:
            value = t.value.strip().lower()
            if not is_valid_address(value):
                raise ValidationFailure(f"invalid target address {t.value!r} in rule for {key!r}")
            if (t.kind, value) not in seen_targets:
                seen_targets.add((t.kind, value))
                normalized.append(Target(kind=t.kind, value=value))
        targets = tuple(normalized)
    return Rule(match_type=match_type, match_key=key, action=action, targets=targets)


def normalize_rules(raw: list[RuleIn]) -> list[Rule]:
    """把请求里的规则规范化为内部 Rule，并做编辑期校验。

    - 键与目标统一小写、去空白；
    - 无效地址 / 域、重复规则、空目标（route 无目标、reject 带目标）一律拒绝。
    """
    rules: list[Rule] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in raw:
        key = _normalize_key(item.match_type, item.match_key)
        dedup = (item.match_type, key)
        if dedup in seen_keys:
            raise ValidationFailure(f"duplicate rule for {item.match_type} {key!r}")
        seen_keys.add(dedup)
        rules.append(_normalize_rule(item.match_type, key, item.action, item.targets))

    return rules


def normalize_ops(raw: list[DraftOp]) -> tuple[dict[tuple[str, str], Rule], list[tuple[str, str]]]:
    """把一组按键操作规范化为 (set_rules, delete_keys)，键均为 (match_type, 规范化键)。

    规则键与 set 内容沿用 normalize_rules 的规范化与校验；以下情况整组拒绝：
    - 重复操作键（规范化后相同，无论 set/delete 组合）；
    - delete 携带 action 或 targets、set 缺少 action；
    - 无效规则键或无效 set 内容。
    """
    set_rules: dict[tuple[str, str], Rule] = {}
    delete_keys: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    for item in raw:
        key = _normalize_key(item.match_type, item.match_key)
        dedup = (item.match_type, key)
        if dedup in seen_keys:
            raise ValidationFailure(f"duplicate op for {item.match_type} {key!r}")
        seen_keys.add(dedup)

        if item.op == "delete":
            if item.action is not None or item.targets:
                raise ValidationFailure(f"delete op for {item.match_type} {key!r} must not carry action or targets")
            delete_keys.append(dedup)
        else:
            if item.action is None:
                raise ValidationFailure(f"set op for {item.match_type} {key!r} requires an action")
            set_rules[dedup] = _normalize_rule(item.match_type, key, item.action, item.targets)

    return set_rules, delete_keys

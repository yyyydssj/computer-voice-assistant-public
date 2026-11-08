"""P5 批次 3：执行前冻结事实复检与授权最终消费。

本模块只产生不可执行的复检通过事实。它不创建 Execution Ledger 记录、不生成
attempt_id、不调用工具、不改变文件系统，也不启动应用或子进程。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.contracts import AuthorizationSnapshot
from app.contracts.base import parse_utc_timestamp, require_choice, require_identifier, require_unique_identifiers
from app.plans import ActionPlanError, PlanAction, ReviewableActionPlan, serialize_plan_json
from app.safety.confirmation.consumption import AuthorizationConsumptionError, AuthorizationConsumptionStore
from app.safety.path_guard import PathGuardError, PathSnapshot
from app.safety.policy_engine import POLICY_VERSION, PolicyEngine, PolicyInput
from app.tools.manifest import ManifestError, ToolRegistry
from app.tools.write_plans import WritePlanError, build_write_action_preview

_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TargetStateHasher = Callable[[ReviewableActionPlan], str]
ContentHashReader = Callable[[str], str]
PathRevalidator = Callable[[PathSnapshot], PathSnapshot]
ApplicationResolver = Callable[[str], str]


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class PreflightError(ValueError):
    """复检失败；错误码稳定且不携带执行能力。"""

    def __init__(self, code: str, message: str, *, action_id: str | None = None, cause_code: str | None = None) -> None:
        self.code = code
        self.message = message
        self.action_id = action_id
        self.cause_code = cause_code
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.action_id is not None:
            result["action_id"] = self.action_id
        if self.cause_code is not None:
            result["cause_code"] = self.cause_code
        return result


@dataclass(frozen=True, slots=True)
class PreflightActionBoundary:
    """执行前冻结的动作工具与取消能力声明；不包含执行入口。"""

    action_id: str
    tool_name: str
    manifest_hash: str
    cancellation_mode: str

    def __post_init__(self) -> None:
        require_identifier("PreflightActionBoundary.action_id", self.action_id)
        require_identifier("PreflightActionBoundary.tool_name", self.tool_name)
        require_identifier("PreflightActionBoundary.manifest_hash", self.manifest_hash)
        require_choice("PreflightActionBoundary.cancellation_mode", self.cancellation_mode, {"cooperative", "not_supported"})


@dataclass(frozen=True, slots=True)
class ExecutionPreflightApproval:
    """全部复检成功且授权已消费后的只读交接事实，不是执行记录。"""

    instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    action_ids: tuple[str, ...]
    checked_at: str
    authorization: AuthorizationSnapshot
    action_boundaries: tuple[PreflightActionBoundary, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("instance_id", self.instance_id), ("session_id", self.session_id),
                            ("operation_id", self.operation_id), ("plan_id", self.plan_id)):
            require_identifier(name, value)
        parse_utc_timestamp("checked_at", self.checked_at)
        if not isinstance(self.authorization, AuthorizationSnapshot) or self.authorization.consumed_at != self.checked_at:
            raise TypeError("authorization 必须是在本次复检中消费的 AuthorizationSnapshot")
        if self.action_ids != self.authorization.allowed_action_ids:
            raise ValueError("action_ids 必须与已消费授权完全一致")
        if not self.action_boundaries:
            object.__setattr__(self, "action_boundaries", tuple(PreflightActionBoundary(action_id=action_id, tool_name="legacy_unbound", manifest_hash="manifest_unbound", cancellation_mode="not_supported") for action_id in self.action_ids))
        if tuple(item.action_id for item in self.action_boundaries) != self.action_ids:
            raise ValueError("action_boundaries 必须与 action_ids 保持相同顺序")
        require_unique_identifiers("action_boundaries.action_id", tuple(item.action_id for item in self.action_boundaries))


class ExecutionPreflightGate:
    """在任何执行事实出现前重新校验整份计划，并最后原子消费授权。"""

    __slots__ = ("instance_id", "_registry", "_store", "_policy", "_target_state_hasher", "_content_hash_reader", "_application_resolver")

    def __init__(self, *, instance_id: str, registry: ToolRegistry,
                 authorization_store: AuthorizationConsumptionStore,
                 path_revalidator: PathRevalidator, target_state_hasher: TargetStateHasher,
                 content_hash_reader: ContentHashReader,
                 application_resolver: ApplicationResolver | None = None) -> None:
        require_identifier("instance_id", instance_id)
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry 必须是 ToolRegistry")
        if not isinstance(authorization_store, AuthorizationConsumptionStore):
            raise TypeError("authorization_store 必须是 AuthorizationConsumptionStore")
        if authorization_store.instance_id != instance_id:
            raise ValueError("复检门与授权存储必须属于同一进程实例")
        for name, value in (("path_revalidator", path_revalidator), ("target_state_hasher", target_state_hasher),
                            ("content_hash_reader", content_hash_reader)):
            if not callable(value):
                raise TypeError(f"{name} 必须可调用")
        self.instance_id = instance_id
        self._registry = registry
        self._store = authorization_store
        self._policy = PolicyEngine(registry, path_revalidator=path_revalidator)
        if application_resolver is not None and not callable(application_resolver):
            raise TypeError("application_resolver 必须可调用")
        self._target_state_hasher = target_state_hasher
        self._content_hash_reader = content_hash_reader
        self._application_resolver = application_resolver

    def check(self, *, authorization: AuthorizationSnapshot, session_id: str, operation_id: str,
              current_plan: ReviewableActionPlan, checked_at: str) -> ExecutionPreflightApproval:
        if not isinstance(authorization, AuthorizationSnapshot):
            raise TypeError("authorization 必须是 AuthorizationSnapshot")
        if not isinstance(current_plan, ReviewableActionPlan):
            raise TypeError("current_plan 必须是 ReviewableActionPlan")
        parse_utc_timestamp("checked_at", checked_at)
        action_ids = tuple(item.action_id for item in current_plan.actions)
        try:
            self._require_authorization_binding(authorization, session_id=session_id, operation_id=operation_id,
                                                current_plan=current_plan, action_ids=action_ids, checked_at=checked_at)
            self._require_plan_integrity(current_plan)
            for action in current_plan.actions:
                self._revalidate_action(action)
            action_boundaries = tuple(PreflightActionBoundary(action_id=action.action_id, tool_name=action.tool_name, manifest_hash=self._registry.require(action.tool_name).content_hash, cancellation_mode=self._registry.require(action.tool_name).cancellation) for action in current_plan.actions)
            current_target_state_hash = self._read_target_state_hash(current_plan)
            if current_target_state_hash != authorization.target_state_hash:
                raise PreflightError("target_state_changed", "执行前目标状态哈希与授权快照不一致")
            try:
                consumed = self._store.consume(
                    authorization,
                    issuer_instance_id=self.instance_id,
                    session_id=session_id,
                    operation_id=operation_id,
                    plan_id=current_plan.plan_id,
                    plan_version=current_plan.plan_version,
                    plan_hash=current_plan.plan_hash,
                    target_state_hash=current_target_state_hash,
                    action_ids=action_ids,
                    consumed_at=checked_at,
                )
            except AuthorizationConsumptionError as exc:
                raise PreflightError(exc.code, exc.message, cause_code=exc.code) from exc
        except PreflightError:
            self._invalidate_if_possible(authorization)
            raise
        return ExecutionPreflightApproval(
            instance_id=self.instance_id,
            session_id=session_id,
            operation_id=operation_id,
            plan_id=current_plan.plan_id,
            plan_version=current_plan.plan_version,
            plan_hash=current_plan.plan_hash,
            target_state_hash=current_target_state_hash,
            action_ids=action_ids,
            checked_at=checked_at,
            authorization=consumed,
            action_boundaries=action_boundaries,
        )

    def _require_authorization_binding(self, authorization: AuthorizationSnapshot, *, session_id: str,
                                       operation_id: str, current_plan: ReviewableActionPlan,
                                       action_ids: tuple[str, ...], checked_at: str) -> None:
        if authorization.issuer_instance_id != self.instance_id:
            raise PreflightError("authorization_wrong_instance", "授权不属于当前进程实例")
        if any(action.risk_level != authorization.risk_level for action in current_plan.actions):
            raise PreflightError("authorization_risk_mismatch", "授权风险等级与计划动作集合不一致")
        if not authorization.is_valid_for(
            issuer_instance_id=self.instance_id,
            session_id=session_id,
            operation_id=operation_id,
            plan_id=current_plan.plan_id,
            plan_version=current_plan.plan_version,
            plan_hash=current_plan.plan_hash,
            target_state_hash=authorization.target_state_hash,
            action_ids=action_ids,
            now=checked_at,
        ):
            raise PreflightError("authorization_facts_mismatch", "授权与当前实例、会话、操作、计划或动作集合不一致或已过期")

    @staticmethod
    def _require_plan_integrity(plan: ReviewableActionPlan) -> None:
        try:
            serialize_plan_json(plan)
        except (ActionPlanError, TypeError, ValueError) as exc:
            code = exc.code if isinstance(exc, ActionPlanError) else type(exc).__name__
            raise PreflightError("plan_integrity_failed", "当前计划无法通过稳定哈希和结构复检", cause_code=code) from exc

    def _revalidate_action(self, action: PlanAction) -> None:
        try:
            manifest = self._registry.require(action.tool_name)
        except ManifestError as exc:
            raise PreflightError("manifest_revalidation_failed", "动作工具已不在当前本地注册表中",
                                 action_id=action.action_id, cause_code="tool_not_registered") from exc
        expected_manifest = (manifest.version, manifest.content_hash, manifest.risk_level,
                             manifest.requires_confirmation, manifest.verification_method, manifest.rollback_strategy)
        frozen_manifest = (action.tool_version, action.manifest_hash, action.risk_level,
                           action.requires_confirmation, action.verification_method, action.rollback_strategy)
        if frozen_manifest != expected_manifest:
            raise PreflightError("manifest_revalidation_failed", "动作与当前 Tool Manifest 不一致", action_id=action.action_id)
        if action.policy_decision.policy_version != POLICY_VERSION:
            raise PreflightError("policy_revalidation_failed", "动作 Policy 版本不是当前固定版本", action_id=action.action_id)

        policy_input = PolicyInput(
            tool_name=action.tool_name,
            tool_version=action.tool_version,
            manifest_hash=action.manifest_hash,
            risk_level=action.risk_level,
            requires_confirmation=action.requires_confirmation,
            normalized_arguments=action.normalized_arguments,
            path_snapshots=action.path_snapshots,
        )
        try:
            current_decision = self._policy.evaluate(policy_input)
        except Exception as exc:
            cause = exc.code if isinstance(exc, PathGuardError) else type(exc).__name__
            raise PreflightError("policy_revalidation_failed", "当前 Policy 复检异常", action_id=action.action_id,
                                 cause_code=cause) from exc
        if current_decision.decision == "deny":
            code = "path_revalidation_failed" if current_decision.reason_code == "path_snapshot_changed" else "policy_revalidation_failed"
            raise PreflightError(code, "动作未通过当前路径或策略复检", action_id=action.action_id,
                                 cause_code=current_decision.reason_code)
        if current_decision != action.policy_decision:
            raise PreflightError("policy_revalidation_failed", "当前 Policy 决策与冻结动作事实不一致", action_id=action.action_id)
        self._validate_action_hashes(action)
        self._rebuild_normalized_action(action)

    def _rebuild_normalized_action(self, action: PlanAction) -> None:
        snapshots = {(item.purpose, item.normalized_path): item for item in action.path_snapshots}

        def validate_path(value: str, purpose: str):
            from pathlib import Path
            candidate = snapshots.get((purpose, value))
            if candidate is None:
                raise WritePlanError("path_snapshot_mismatch", "重建参数时路径与冻结快照不一致")
            return Path(candidate.normalized_path)

        arguments = _thaw_json(action.normalized_arguments)
        try:
            if action.tool_name == "start_whitelisted_application":
                if self._application_resolver is None:
                    raise WritePlanError("application_resolver_missing", "应用动作复检必须提供当前白名单解析器")
                rebuilt = build_write_action_preview(action.tool_name, arguments,
                    registry=self._registry, application_resolver=self._application_resolver)
            else:
                rebuilt = build_write_action_preview(action.tool_name, arguments,
                    registry=self._registry, path_validator=validate_path)
        except Exception as exc:
            cause = exc.code if isinstance(exc, WritePlanError) else type(exc).__name__
            raise PreflightError("parameter_revalidation_failed", "规范化参数无法通过当前工具 Schema 和固定规则重建",
                                 action_id=action.action_id, cause_code=cause) from exc
        if rebuilt.normalized_arguments != action.normalized_arguments or rebuilt.preview != action.preview:
            raise PreflightError("parameter_revalidation_failed", "当前重建的规范化参数或预览与冻结动作不一致",
                                 action_id=action.action_id)


    def _validate_action_hashes(self, action: PlanAction) -> None:
        arguments = action.normalized_arguments
        preview = action.preview
        if action.tool_name == "create_text_file":
            encoding = arguments.get("encoding")
            content = arguments.get("content")
            if not isinstance(encoding, str) or not isinstance(content, str):
                raise PreflightError("action_hash_mismatch", "创建文件参数缺少可校验内容", action_id=action.action_id)
            try:
                payload = content.encode(encoding)
            except (LookupError, UnicodeEncodeError) as exc:
                raise PreflightError("action_hash_mismatch", "创建文件内容编码无法复检", action_id=action.action_id) from exc
            self._require_preview_hash(action, payload, size_key="size_bytes")
            if preview.get("expected_target_state") != "absent" or any(item.exists for item in action.path_snapshots):
                raise PreflightError("target_state_changed", "创建目标不再满足 absent 约束", action_id=action.action_id)
        elif action.tool_name == "write_text_file":
            content = arguments.get("content")
            expected_source_hash = arguments.get("expected_source_hash")
            path = arguments.get("path")
            if not isinstance(content, str) or not self._is_hash(expected_source_hash) or not isinstance(path, str):
                raise PreflightError("action_hash_mismatch", "写入动作缺少可复检参数或源哈希", action_id=action.action_id)
            self._require_preview_hash(action, content.encode("utf-8"), size_key="content_size_bytes")
            if preview.get("expected_source_hash") != expected_source_hash:
                raise PreflightError("action_hash_mismatch", "预览源哈希与规范化参数不一致", action_id=action.action_id)
            if arguments.get("mode") == "replace_exact":
                target_text = arguments.get("target_text")
                if not isinstance(target_text, str) or preview.get("target_text_hash") != self._sha256(target_text.encode("utf-8")):
                    raise PreflightError("action_hash_mismatch", "精确替换目标文本哈希不一致", action_id=action.action_id)
            try:
                current_source_hash = self._content_hash_reader(path)
            except Exception as exc:
                raise PreflightError("source_hash_unavailable", "执行前无法可靠读取源文件哈希",
                                     action_id=action.action_id, cause_code=type(exc).__name__) from exc
            if not self._is_hash(current_source_hash) or current_source_hash != expected_source_hash:
                raise PreflightError("source_hash_changed", "执行前源文件内容哈希已变化", action_id=action.action_id)
        elif action.tool_name == "move_to_recycle_bin":
            paths = arguments.get("paths")
            if not isinstance(paths, tuple) or preview.get("item_count") != len(paths) or tuple(preview.get("paths", ())) != paths:
                raise PreflightError("action_hash_mismatch", "回收站动作数量或完整路径清单不一致", action_id=action.action_id)
            if preview.get("permanent_delete") is not False:
                raise PreflightError("policy_revalidation_failed", "永久删除禁用事实被改变", action_id=action.action_id)

    def _require_preview_hash(self, action: PlanAction, payload: bytes, *, size_key: str) -> None:
        if action.preview.get("content_hash") != self._sha256(payload) or action.preview.get(size_key) != len(payload):
            raise PreflightError("action_hash_mismatch", "写入内容哈希或字节数与规范化参数不一致", action_id=action.action_id)

    def _read_target_state_hash(self, plan: ReviewableActionPlan) -> str:
        try:
            value = self._target_state_hasher(plan)
        except Exception as exc:
            raise PreflightError("target_state_unavailable", "执行前无法可靠计算目标状态哈希",
                                 cause_code=type(exc).__name__) from exc
        if not self._is_hash(value):
            raise PreflightError("target_state_unavailable", "目标状态扫描未返回 sha256 哈希")
        return value

    def _invalidate_if_possible(self, authorization: AuthorizationSnapshot) -> None:
        try:
            self._store.invalidate(authorization)
        except (AuthorizationConsumptionError, TypeError):
            pass

    @staticmethod
    def _sha256(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _is_hash(value: Any) -> bool:
        return isinstance(value, str) and _HASH_PATTERN.fullmatch(value) is not None

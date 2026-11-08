"""P5 批次 2：L3 两阶段确认，固定短语与一次性授权登记。"""
from __future__ import annotations
from dataclasses import dataclass, replace
from typing import Any
from app.contracts import AuthorizationSnapshot
from app.contracts.base import parse_utc_timestamp, require_identifier
from app.plans import ReviewableActionPlan
from .consumption import AuthorizationConsumptionStore
from .l2 import ConfirmationActionSummary, ConfirmationError, ConfirmationResolution, _hash

_EVENTS=frozenset({"allow", "deny", "close", "escape", "stop", "timeout"})
PHRASE="确认执行"

@dataclass(frozen=True, slots=True)
class L3ConfirmationPrompt:
    modal: bool
    allow_label: str
    deny_label: str
    confirmation_phrase: str
    issuer_instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    goal: str
    actions: tuple[ConfirmationActionSummary, ...]
    def __post_init__(self) -> None:
        if self.modal is not True or (self.allow_label, self.deny_label)!=("允许执行","拒绝") or self.confirmation_phrase != PHRASE:
            raise ConfirmationError("invalid_prompt", "L3 提示文案必须固定")
        for name,value in (("issuer_instance_id",self.issuer_instance_id),("session_id",self.session_id),("operation_id",self.operation_id),("plan_id",self.plan_id)):
            try: require_identifier(name,value)
            except (TypeError,ValueError) as exc: raise ConfirmationError("invalid_prompt", f"{name} 无效") from exc
        _hash("plan_hash",self.plan_hash); _hash("target_state_hash",self.target_state_hash)
        actions=tuple(self.actions)
        if not actions or any(action.risk_level!="L3" for action in actions): raise ConfirmationError("l3_plan_required","L3 提示只能包含 L3 动作")
        object.__setattr__(self,"actions",actions)

@dataclass(frozen=True, slots=True)
class L3ConfirmationWindow:
    authorization_id: str
    confirmation_window_id: str
    issuer_instance_id: str
    session_id: str
    operation_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    target_state_hash: str
    allowed_action_ids: tuple[str,...]
    created_at: str
    expires_at: str
    prompt: L3ConfirmationPrompt
    stage: str="modal_pending"
    def __post_init__(self) -> None:
        for name,value in (("authorization_id",self.authorization_id),("confirmation_window_id",self.confirmation_window_id),("issuer_instance_id",self.issuer_instance_id),("session_id",self.session_id),("operation_id",self.operation_id),("plan_id",self.plan_id)):
            try: require_identifier(name,value)
            except (TypeError,ValueError) as exc: raise ConfirmationError("invalid_window",f"{name} 无效") from exc
        if self.stage not in {"modal_pending","awaiting_phrase"}: raise ConfirmationError("invalid_window","L3 确认阶段无效")
        if parse_utc_timestamp("expires_at",self.expires_at) <= parse_utc_timestamp("created_at",self.created_at): raise ConfirmationError("invalid_time_range","expires_at 必须晚于 created_at")
        if tuple(a.action_id for a in self.prompt.actions)!=self.allowed_action_ids: raise ConfirmationError("invalid_window","窗口动作与提示不一致")

class L3ConfirmationController:
    def __init__(self, *, instance_id: str, authorization_store: AuthorizationConsumptionStore|None=None) -> None:
        require_identifier("instance_id",instance_id)
        if authorization_store is not None and authorization_store.instance_id != instance_id: raise ConfirmationError("instance_mismatch","消费存储实例不匹配")
        self._instance_id=instance_id; self._store=authorization_store or AuthorizationConsumptionStore(instance_id=instance_id)
        self._active_window: L3ConfirmationWindow|None=None; self._used_window_ids:set[str]=set(); self._used_authorization_ids:set[str]=set()
    @property
    def active_window(self): return self._active_window

    def begin(self, *, plan:ReviewableActionPlan, session_id:str, operation_id:str, authorization_id:str,
              confirmation_window_id:str, target_state_hash:str, created_at:str, expires_at:str)->L3ConfirmationWindow:
        if self._active_window is not None: raise ConfirmationError("confirmation_already_active","已有活动确认窗口")
        if confirmation_window_id in self._used_window_ids: raise ConfirmationError("confirmation_window_reused","confirmation_window_id 不得复用")
        if authorization_id in self._used_authorization_ids: raise ConfirmationError("authorization_id_reused","authorization_id 不得复用")
        if not isinstance(plan,ReviewableActionPlan): raise TypeError("plan 必须是 ReviewableActionPlan")
        self._validate_plan(plan); _hash("target_state_hash",target_state_hash)
        summaries=tuple(ConfirmationActionSummary(order=a.order,action_id=a.action_id,tool_name=a.tool_name,risk_level=a.risk_level,
            normalized_arguments=a.normalized_arguments,normalized_paths=a.policy_decision.normalized_paths,preview=a.preview,
            verification_method=a.verification_method,rollback_strategy=a.rollback_strategy) for a in plan.actions)
        prompt=L3ConfirmationPrompt(True,"允许执行","拒绝",PHRASE,self._instance_id,session_id,operation_id,plan.plan_id,
            plan.plan_version,plan.plan_hash,target_state_hash,plan.goal,summaries)
        window=L3ConfirmationWindow(authorization_id,confirmation_window_id,self._instance_id,session_id,operation_id,plan.plan_id,
            plan.plan_version,plan.plan_hash,target_state_hash,tuple(a.action_id for a in plan.actions),created_at,expires_at,prompt)
        self._used_window_ids.add(confirmation_window_id); self._used_authorization_ids.add(authorization_id); self._active_window=window
        return window

    def resolve(self, *, confirmation_window_id:str, session_id:str, event:str, occurred_at:str,
                current_plan:ReviewableActionPlan|None=None, current_target_state_hash:str|None=None)->ConfirmationResolution:
        window=self._require_window(confirmation_window_id,session_id); occurred,expires=self._times(window,occurred_at)
        if event not in _EVENTS: raise ConfirmationError("invalid_event","只接受固定的本地确认事件")
        if event=="timeout" and occurred<expires: raise ConfirmationError("timeout_not_reached","确认窗口尚未到期")
        if occurred>=expires: return self._finish(window,"expired","confirmation_expired",occurred_at)
        if event in {"deny","close","escape","stop"}:
            status="denied" if event=="deny" else "cancelled"
            return self._finish(window,status,{"deny":"user_denied","close":"window_closed","escape":"escape_pressed","stop":"user_stopped"}[event],occurred_at)
        if event=="timeout": raise AssertionError
        if window.stage!="modal_pending": raise ConfirmationError("confirmation_stage_mismatch","当前不接受第一阶段允许事件")
        drift=self._drift(window,current_plan,current_target_state_hash)
        if drift: return self._finish(window,"invalidated",drift,occurred_at)
        self._active_window=replace(window,stage="awaiting_phrase")
        return ConfirmationResolution(window.confirmation_window_id,"awaiting_phrase","modal_allowed",occurred_at)

    def submit_phrase(self, *, confirmation_window_id:str, session_id:str, phrase:str, occurred_at:str,
                      current_plan:ReviewableActionPlan|None=None,current_target_state_hash:str|None=None)->ConfirmationResolution:
        window=self._require_window(confirmation_window_id,session_id); occurred,expires=self._times(window,occurred_at)
        if window.stage!="awaiting_phrase": raise ConfirmationError("confirmation_stage_mismatch","必须先完成第一阶段模态确认")
        if occurred>=expires: return self._finish(window,"expired","confirmation_expired",occurred_at)
        if phrase!=PHRASE: return ConfirmationResolution(window.confirmation_window_id,"awaiting_phrase","phrase_mismatch",occurred_at)
        drift=self._drift(window,current_plan,current_target_state_hash)
        if drift: return self._finish(window,"invalidated",drift,occurred_at)
        auth=AuthorizationSnapshot(authorization_id=window.authorization_id,issuer_instance_id=window.issuer_instance_id,
            session_id=window.session_id,operation_id=window.operation_id,plan_id=window.plan_id,plan_version=window.plan_version,
            plan_hash=window.plan_hash,target_state_hash=window.target_state_hash,allowed_action_ids=window.allowed_action_ids,
            risk_level="L3",confirmation_window_id=window.confirmation_window_id,authorization_method="modal_and_phrase",
            confirmation_stage="phrase",confirmation_phrase=PHRASE,created_at=occurred_at,expires_at=window.expires_at)
        self._store.register(auth)
        return self._finish(window,"authorized","phrase_confirmed",occurred_at,auth)

    def _require_window(self,window_id,session_id):
        w=self._active_window
        if w is None: raise ConfirmationError("no_active_confirmation","当前没有活动确认窗口")
        if window_id!=w.confirmation_window_id: raise ConfirmationError("confirmation_window_mismatch","事件不属于当前确认窗口")
        if session_id!=w.session_id: raise ConfirmationError("session_mismatch","确认事件不属于当前会话")
        return w
    def _times(self,w,occurred_at):
        occurred=parse_utc_timestamp("occurred_at",occurred_at); created=parse_utc_timestamp("created_at",w.created_at); expires=parse_utc_timestamp("expires_at",w.expires_at)
        if occurred<created: raise ConfirmationError("event_before_window","确认事件早于窗口创建时间")
        return occurred,expires
    def _drift(self,w,plan,target):
        if plan is None or target is None: raise ConfirmationError("current_facts_required","确认前必须提供当前计划和目标状态事实")
        if not isinstance(plan,ReviewableActionPlan): raise TypeError("current_plan 必须是 ReviewableActionPlan")
        _hash("current_target_state_hash",target)
        identity=(plan.plan_id,plan.plan_version,plan.plan_hash,tuple(a.action_id for a in plan.actions))
        if identity!=(w.plan_id,w.plan_version,w.plan_hash,w.allowed_action_ids): return "plan_changed"
        try: self._validate_plan(plan)
        except ConfirmationError: return "plan_changed"
        return "target_state_changed" if target!=w.target_state_hash else None
    @staticmethod
    def _validate_plan(plan):
        if any(a.risk_level!="L3" or a.requires_confirmation is not True or a.policy_decision.decision!="require_confirmation" for a in plan.actions):
            raise ConfirmationError("l3_plan_required","L3 确认只接受全部动作为 L3 的计划")
    def _finish(self,w,status,reason,at,auth=None):
        self._active_window=None; return ConfirmationResolution(w.confirmation_window_id,status,reason,at,auth)

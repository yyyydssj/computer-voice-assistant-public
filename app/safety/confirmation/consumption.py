"""进程实例内的一次性授权消费、失效与防重放。"""
from __future__ import annotations
from threading import Lock
from app.contracts import AuthorizationSnapshot
from app.contracts.base import require_identifier


class AuthorizationConsumptionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code; self.message = message; super().__init__(message)


class AuthorizationConsumptionStore:
    """只保存当前进程实例创建的授权；重启后旧授权默认未知。"""
    def __init__(self, *, instance_id: str) -> None:
        require_identifier("instance_id", instance_id)
        self.instance_id = instance_id
        self._known: dict[str, str] = {}
        self._consumed: set[str] = set()
        self._invalidated: set[str] = set()
        self._lock = Lock()

    def register(self, authorization: AuthorizationSnapshot) -> None:
        if authorization.issuer_instance_id != self.instance_id:
            raise AuthorizationConsumptionError("authorization_wrong_instance", "不能登记其他实例签发的授权")
        if authorization.consumed_at is not None:
            raise AuthorizationConsumptionError("authorization_already_consumed", "不能登记已消费授权")
        with self._lock:
            previous = self._known.get(authorization.authorization_id)
            current = authorization.stable_hash()
            if previous is not None and previous != current:
                raise AuthorizationConsumptionError("authorization_id_collision", "authorization_id 对应事实冲突")
            self._known[authorization.authorization_id] = current

    def consume(self, authorization: AuthorizationSnapshot, *, issuer_instance_id: str, session_id: str,
                operation_id: str, plan_id: str, plan_version: int, plan_hash: str,
                target_state_hash: str, action_ids: tuple[str, ...], consumed_at: str) -> AuthorizationSnapshot:
        if not isinstance(authorization, AuthorizationSnapshot):
            raise TypeError("authorization 必须是 AuthorizationSnapshot")
        if authorization.issuer_instance_id != self.instance_id or issuer_instance_id != self.instance_id:
            raise AuthorizationConsumptionError("authorization_wrong_instance", "授权不属于当前进程实例")
        with self._lock:
            known_hash = self._known.get(authorization.authorization_id)
            if known_hash is None:
                raise AuthorizationConsumptionError("authorization_unknown", "当前实例未签发此授权")
            if authorization.authorization_id in self._consumed:
                raise AuthorizationConsumptionError("authorization_replayed", "授权已被消费，不得重放")
            if authorization.authorization_id in self._invalidated:
                raise AuthorizationConsumptionError("authorization_invalidated", "授权已因执行前复检失败而失效")
            if known_hash != authorization.stable_hash():
                raise AuthorizationConsumptionError("authorization_tampered", "授权快照已被修改")
            if not authorization.is_valid_for(issuer_instance_id=issuer_instance_id, session_id=session_id,
                    operation_id=operation_id, plan_id=plan_id, plan_version=plan_version, plan_hash=plan_hash,
                    target_state_hash=target_state_hash, action_ids=action_ids, now=consumed_at):
                raise AuthorizationConsumptionError("authorization_facts_mismatch", "授权与当前冻结事实不一致或已过期")
            consumed = authorization.consume(consumed_at)
            self._consumed.add(authorization.authorization_id)
            return consumed

    def invalidate(self, authorization: AuthorizationSnapshot) -> None:
        """原子使当前实例登记的未消费授权永久失效。"""
        if not isinstance(authorization, AuthorizationSnapshot):
            raise TypeError("authorization 必须是 AuthorizationSnapshot")
        if authorization.issuer_instance_id != self.instance_id:
            raise AuthorizationConsumptionError("authorization_wrong_instance", "授权不属于当前进程实例")
        with self._lock:
            known_hash = self._known.get(authorization.authorization_id)
            if known_hash is None:
                raise AuthorizationConsumptionError("authorization_unknown", "当前实例未签发此授权")
            if known_hash != authorization.stable_hash():
                raise AuthorizationConsumptionError("authorization_tampered", "授权快照已被修改")
            if authorization.authorization_id in self._consumed:
                raise AuthorizationConsumptionError("authorization_replayed", "授权已被消费，不得再次失效")
            self._invalidated.add(authorization.authorization_id)
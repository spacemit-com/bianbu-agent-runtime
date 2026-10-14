"""受 Security Runtime 策略约束的 Agent child 生命周期 API。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from bianbu_agent.api.contracts import (
    ExecutionCollectionEnvelope,
    ExecutionControlEnvelope,
    ExecutionControlView,
    ExecutionEnvelope,
    ExecutionRevokeRequest,
    ExecutionStartRequest,
    ExecutionStopRequest,
    ExecutionView,
)


router = APIRouter()


def _view(record: object) -> ExecutionView:
    if hasattr(record, "as_dict"):
        record = record.as_dict()  # type: ignore[assignment]
    return ExecutionView.model_validate(record)


@router.get("/executions", response_model=ExecutionCollectionEnvelope)
async def list_executions(request: Request) -> ExecutionCollectionEnvelope:
    runtime = request.app.state.runtime
    return ExecutionCollectionEnvelope(
        data=[_view(item) for item in runtime.executions.list()]
    )


@router.get("/executions/{instance_id}", response_model=ExecutionEnvelope)
async def get_execution(instance_id: str, request: Request) -> ExecutionEnvelope:
    runtime = request.app.state.runtime
    for item in runtime.executions.list():
        if str(item.get("instance_id")) == instance_id:
            return ExecutionEnvelope(data=_view(item))
    raise KeyError(instance_id)


@router.post(
    "/agents/{agent_id}/executions",
    status_code=201,
    response_model=ExecutionEnvelope,
)
async def start_execution(
    agent_id: str,
    payload: ExecutionStartRequest,
    request: Request,
) -> ExecutionEnvelope:
    runtime = request.app.state.runtime
    record = await runtime.start_agent_execution(
        agent_id,
        requested_mode=payload.requested_mode,
        env=payload.env,
        target_port=payload.target_port,
        listen_port=payload.listen_port,
    )
    return ExecutionEnvelope(data=_view(record))


@router.post(
    "/executions/{instance_id}/stop",
    response_model=ExecutionControlEnvelope,
)
async def stop_execution(
    instance_id: str,
    payload: ExecutionStopRequest,
    request: Request,
) -> ExecutionControlEnvelope:
    accepted = await request.app.state.runtime.stop_execution(
        instance_id, reason=payload.reason
    )
    if not accepted:
        raise KeyError(instance_id)
    return ExecutionControlEnvelope(data=ExecutionControlView(accepted=True))


@router.post(
    "/agents/{agent_id}/revoke",
    response_model=ExecutionControlEnvelope,
)
async def revoke_agent(
    agent_id: str,
    payload: ExecutionRevokeRequest,
    request: Request,
) -> ExecutionControlEnvelope:
    await request.app.state.runtime.revoke_agent_execution(
        agent_id, reason=payload.reason
    )
    return ExecutionControlEnvelope(data=ExecutionControlView(accepted=True))


__all__ = ["router"]

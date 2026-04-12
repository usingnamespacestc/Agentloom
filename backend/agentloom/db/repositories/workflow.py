"""Workflow repository — persist and load schemas.WorkFlow via JSONB."""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.workflow import WorkflowRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas import WorkFlow
from agentloom.schemas.common import FrozenNodeError


class WorkflowNotFoundError(KeyError):
    pass


class WorkflowRepository(WorkspaceScopedRepository):
    """CRUD for WorkflowRow.

    All reads scope by ``workspace_id``. Attempting to load a workflow
    belonging to another workspace raises ``WorkflowNotFoundError`` (not
    a permission error — from this repository's point of view the row
    does not exist).
    """

    async def create(self, workflow: WorkFlow, owner_id: str | None = None) -> WorkflowRow:
        row = WorkflowRow(
            id=workflow.id,
            workspace_id=self.workspace_id,
            owner_id=owner_id,
            payload=workflow.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, workflow_id: str) -> WorkFlow:
        stmt = (
            select(WorkflowRow)
            .where(WorkflowRow.workspace_id == self.workspace_id)
            .where(WorkflowRow.id == workflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WorkflowNotFoundError(workflow_id)
        return WorkFlow.model_validate(row.payload)

    async def save(self, workflow: WorkFlow) -> None:
        """Overwrite an existing row's payload. Enforces frozen-node
        invariant by diffing against the prior payload: any node that
        was frozen in the prior version and differs in the new version
        is a rejection."""
        stmt = (
            select(WorkflowRow)
            .where(WorkflowRow.workspace_id == self.workspace_id)
            .where(WorkflowRow.id == workflow.id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WorkflowNotFoundError(workflow.id)

        prior = WorkFlow.model_validate(row.payload)
        _assert_frozen_nodes_unchanged(prior, workflow)

        row.payload = workflow.model_dump(mode="json")
        await self.session.flush()

    async def list_ids(self) -> list[str]:
        stmt = (
            select(WorkflowRow.id)
            .where(WorkflowRow.workspace_id == self.workspace_id)
            .order_by(WorkflowRow.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


def _assert_frozen_nodes_unchanged(prior: WorkFlow, new: WorkFlow) -> None:
    """Reject any change to a node that was frozen in ``prior``."""
    for nid, prior_node in prior.nodes.items():
        if not prior_node.is_frozen:
            continue
        new_node = new.nodes.get(nid)
        if new_node is None:
            raise FrozenNodeError(f"Node {nid} is frozen and may not be deleted")
        if new_node.model_dump(mode="json") != prior_node.model_dump(mode="json"):
            raise FrozenNodeError(f"Node {nid} is frozen and may not be modified")

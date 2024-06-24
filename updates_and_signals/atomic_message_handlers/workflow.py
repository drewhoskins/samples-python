import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Optional

from temporalio import activity, common, workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from updates_and_signals.atomic_message_handlers.activities import (
    AllocateNodesToJobInput,
    DeallocateNodesForJobInput,
    FindBadNodesInput,
    allocate_nodes_to_job,
    deallocate_nodes_for_job,
    find_bad_nodes,
)


# In workflows that continue-as-new, it's convenient to store all your state in one serializable structure
# to make it easier to pass between runs
@dataclass(kw_only=True)
class ClusterManagerState:
    cluster_started: bool = False
    cluster_shutdown: bool = False
    nodes: Optional[Dict[str, Optional[str]]] = None
    max_assigned_nodes: int = 0


@dataclass(kw_only=True)
class ClusterManagerInput:
    state: Optional[ClusterManagerState] = None
    test_continue_as_new: bool = False


@dataclass
class ClusterManagerResult:
    max_assigned_nodes: int
    num_currently_assigned_nodes: int


@dataclass(kw_only=True)
class ClusterManagerAllocateNNodesToJobInput:
    num_nodes: int
    task_name: str


@dataclass(kw_only=True)
class ClusterManagerDeleteJobInput:
    task_name: str


# ClusterManagerWorkflow keeps track of the allocations of a cluster of nodes.
# Via signals, the cluster can be started and shutdown.
# Via updates, clients can also assign jobs to nodes and delete jobs.
# These updates must run atomically.
@workflow.defn
class ClusterManagerWorkflow:
    def __init__(self) -> None:
        self.state = ClusterManagerState()
        # Protects workflow state from interleaved access
        self.nodes_lock = asyncio.Lock()

    @workflow.signal
    async def start_cluster(self):
        self.state.cluster_started = True
        self.state.nodes = {str(k): None for k in range(25)}
        workflow.logger.info("Cluster started")

    @workflow.signal
    async def shutdown_cluster(self):
        await workflow.wait_condition(lambda: self.state.cluster_started)
        self.state.cluster_shutdown = True
        workflow.logger.info("Cluster shut down")

    @workflow.update
    async def allocate_n_nodes_to_job(
        self, input: ClusterManagerAllocateNNodesToJobInput
    ) -> List[str]:
        await workflow.wait_condition(lambda: self.state.cluster_started)
        if self.state.cluster_shutdown:
            # If you want the client to receive a failure, either add an update validator and throw the
            # exception from there, or raise an ApplicationError. Other exceptions in the main handler
            # will cause the workflow to keep retrying and get it stuck.
            raise ApplicationError(
                "Cannot allocate nodes to a job: Cluster is already shut down"
            )

        async with self.nodes_lock:
            unassigned_nodes = [k for k, v in self.state.nodes.items() if v is None]
            if len(unassigned_nodes) < input.num_nodes:
                # If you want the client to receive a failure, either add an update validator and throw the
                # exception from there, or raise an ApplicationError. Other exceptions in the main handler
                # will cause the workflow to keep retrying and get it stuck.
                raise ApplicationError(
                    f"Cannot allocate {input.num_nodes} nodes; have only {len(unassigned_nodes)} available"
                )
            assigned_nodes = unassigned_nodes[: input.num_nodes]
            # This await would be dangerous without nodes_lock because it yields control and allows interleaving.
            await self._allocate_nodes_to_job(assigned_nodes, input.task_name)
            self.state.max_assigned_nodes = max(
                self.state.max_assigned_nodes,
                len([k for k, v in self.state.nodes.items() if v is not None]),
            )
            return assigned_nodes

    async def _allocate_nodes_to_job(self, assigned_nodes: List[str], task_name: str):
        await workflow.execute_activity(
            allocate_nodes_to_job,
            AllocateNodesToJobInput(nodes=assigned_nodes, task_name=task_name),
            start_to_close_timeout=timedelta(seconds=10),
        )
        for node in assigned_nodes:
            self.state.nodes[node] = task_name

    @workflow.update
    async def delete_job(self, input: ClusterManagerDeleteJobInput):
        await workflow.wait_condition(lambda: self.state.cluster_started)
        if self.state.cluster_shutdown:
            # If you want the client to receive a failure, either add an update validator and throw the
            # exception from there, or raise an ApplicationError. Other exceptions in the main handler
            # will cause the workflow to keep retrying and get it stuck.
            raise ApplicationError("Cannot delete a job: Cluster is already shut down")

        async with self.nodes_lock:
            nodes_to_free = [
                k for k, v in self.state.nodes.items() if v == input.task_name
            ]
            # This await would be dangerous without nodes_lock because it yields control and allows interleaving.
            await self._deallocate_nodes_for_job(nodes_to_free, input.task_name)

    async def _deallocate_nodes_for_job(self, nodes_to_free: List[str], task_name: str):
        await workflow.execute_activity(
            deallocate_nodes_for_job,
            DeallocateNodesForJobInput(nodes=nodes_to_free, task_name=task_name),
            start_to_close_timeout=timedelta(seconds=10),
        )
        for node in nodes_to_free:
            self.state.nodes[node] = None

    def get_assigned_nodes(self) -> List[str]:
        return [k for k, v in self.state.nodes.items() if v is not None and v != "BAD!"]

    async def perform_health_checks(self):
        async with self.nodes_lock:
            assigned_nodes = self.get_assigned_nodes()
            # This await would be dangerous without nodes_lock because it yields control and allows interleaving.
            bad_nodes = await workflow.execute_activity(
                find_bad_nodes,
                FindBadNodesInput(nodes_to_check=assigned_nodes),
                start_to_close_timeout=timedelta(seconds=10),
                # This health check is optional, and our lock would block the whole workflow if we let it retry forever.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            for node in bad_nodes:
                self.state.nodes[node] = "BAD!"

    # The cluster manager is a long-running "entity" workflow so we need to periodically checkpoint its state and
    # continue-as-new.
    def init(self, input: ClusterManagerInput):
        if input.state:
            self.state = input.state
        if input.test_continue_as_new:
            self.max_history_length = 120
            self.sleep_interval_seconds = 1
        else:
            self.max_history_length = None
            self.sleep_interval_seconds = 600

    def should_continue_as_new(self):
        # We don't want to continue-as-new if we're in the middle of an update
        if self.nodes_lock.locked():
            return False
        if workflow.info().is_continue_as_new_suggested():
            return True
        # This is just for ease-of-testing.  In production, we trust temporal to tell us when to continue as new.
        if (
            self.max_history_length
            and workflow.info().get_current_history_length() > self.max_history_length
        ):
            return True
        return False

    # max_history_size - to more conveniently test continue-as-new, not to be used in production.
    @workflow.run
    async def run(self, input: ClusterManagerInput) -> ClusterManagerResult:
        self.init(input)
        await workflow.wait_condition(lambda: self.state.cluster_started)

        # Perform health checks at intervals.
        while True:
            await self.perform_health_checks()
            try:
                await workflow.wait_condition(
                    lambda: self.state.cluster_shutdown
                    or self.should_continue_as_new(),
                    timeout=timedelta(seconds=self.sleep_interval_seconds),
                )
            except asyncio.TimeoutError:
                pass
            if self.state.cluster_shutdown:
                break
            if self.should_continue_as_new():
                workflow.logger.info("Continuing as new")
                await workflow.continue_as_new(
                    ClusterManagerInput(
                        state=self.state,
                        test_continue_as_new=input.test_continue_as_new,
                    )
                )

        return ClusterManagerResult(
            self.state.max_assigned_nodes, len(self.get_assigned_nodes())
        )
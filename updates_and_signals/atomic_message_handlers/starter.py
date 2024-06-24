import argparse
import asyncio
import logging
import uuid
from typing import Optional

from temporalio import client, common
from temporalio.client import Client, WorkflowHandle

from updates_and_signals.atomic_message_handlers.workflow import (
    ClusterManagerAllocateNNodesToJobInput,
    ClusterManagerDeleteJobInput,
    ClusterManagerInput,
    ClusterManagerWorkflow,
)


async def do_cluster_lifecycle(wf: WorkflowHandle, delay_seconds: Optional[int] = None):
    
    await wf.signal(ClusterManagerWorkflow.start_cluster)

    allocation_updates = []
    for i in range(6):
        allocation_updates.append(
            wf.execute_update(
                ClusterManagerWorkflow.allocate_n_nodes_to_job, 
                ClusterManagerAllocateNNodesToJobInput(num_nodes=2, task_name=f"task-{i}")
            )
        )
    await asyncio.gather(*allocation_updates)

    if delay_seconds:
        await asyncio.sleep(delay_seconds)

    deletion_updates = []
    for i in range(6):
        deletion_updates.append(
            wf.execute_update(
                ClusterManagerWorkflow.delete_job, 
                ClusterManagerDeleteJobInput(task_name=f"task-{i}")
            )
        )
    await asyncio.gather(*deletion_updates)

    await wf.signal(ClusterManagerWorkflow.shutdown_cluster)


async def main(should_test_continue_as_new: bool):
    # Connect to Temporal
    client = await Client.connect("localhost:7233")

    cluster_manager_handle = await client.start_workflow(
        ClusterManagerWorkflow.run,
        ClusterManagerInput(test_continue_as_new=should_test_continue_as_new),
        id=f"ClusterManagerWorkflow-{uuid.uuid4()}",
        task_queue="atomic-message-handlers-task-queue",
        id_reuse_policy=common.WorkflowIDReusePolicy.TERMINATE_IF_RUNNING,
    )
    delay_seconds = 10 if should_test_continue_as_new else 1
    await do_cluster_lifecycle(cluster_manager_handle, delay_seconds=delay_seconds)
    result = await cluster_manager_handle.result()
    print(
        f"Cluster shut down successfully.  It peaked at {result.max_assigned_nodes} assigned nodes ."
        f" It had {result.num_currently_assigned_nodes} nodes assigned at the end."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Atomic message handlers")
    parser.add_argument(
        "--test-continue-as-new",
        help="Make the ClusterManagerWorkflow continue as new before shutting down",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()
    asyncio.run(main(args.test_continue_as_new))

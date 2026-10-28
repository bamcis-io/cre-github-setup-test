"""The client metrics contract, for any locustfile in the LoadGenerator image.

DemoObservability's availability graph, its ClientAvailabilityAlarm and the FIS stop condition
read ClientSuccess / ClientError / ClientLatency with no dimensions. Each request prints ONE
Embedded Metric Format line to stdout: the Fargate awslogs driver ships it to CloudWatch Logs,
which extracts the metrics into ``DEMO_METRIC_NAMESPACE`` (the env var the stack injects).

It is a module of its own, not part of the base locustfile, because a ``workloadDirectory``
locustfile REPLACES the base one: an overlay imports this instead of copying it (review 10c F1).

    from cre_emf import emit
    emit(ok, elapsed_ms, "POST /work")     # once per request, success or failure
"""

import json
import os
import time

METRIC_NAMESPACE = os.environ.get("DEMO_METRIC_NAMESPACE", "MyResilienceDemo")


def client_emf(ok: bool, elapsed_ms: float, target: str) -> dict:
    """The EMF record for one request, published with no dimensions (the fleet) and by Target."""
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [[], ["Target"]],
                    "Metrics": [
                        {"Name": "ClientLatency", "Unit": "Milliseconds"},
                        {"Name": "ClientSuccess", "Unit": "Count"},
                        {"Name": "ClientError", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Target": target,
        "ClientLatency": elapsed_ms,
        "ClientSuccess": 1 if ok else 0,
        "ClientError": 0 if ok else 1,
    }


def emit(ok: bool, elapsed_ms: float, target: str) -> None:
    """Print one request's EMF line. Call it once per request, so a request is counted once."""
    print(json.dumps(client_emf(ok, elapsed_ms, target)), flush=True)

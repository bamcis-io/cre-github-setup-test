---
inclusion: auto
name: cre-load-generation
description: Driving load with LoadGenerator (Locust on Fargate): closed-loop vs open-loop locustfiles, the workload overlay and its packages, starting and stopping a run, ramping and sizing. Read when writing a locustfile, choosing a load model, or running a load test.
---
# Load generation: closed loop or open loop

`LoadGenerator` runs Locust on Fargate (`cre-constructs.md`, load-generation). How the locustfile
issues requests decides what a failure looks like on the dashboard, so pick the model with the
story in mind and say which one in the design.

## Closed loop (sequential), as the base locustfile does

Each user sends a request, **waits for the response**, waits `wait_time`, then sends the next. The
request rate is `users / (latency + wait)`. The base image's `locustfile.py` is this shape:
`LoadUser(HttpUser)` with `wait_time = between(MIN_WAIT, MAX_WAIT)` (`LoadGenerator`'s
`minWaitSeconds` / `maxWaitSeconds`), one request to `targetPath` with `httpMethod`, and one EMF
line per request.

When the backend slows down, every user waits longer, so the request rate **falls** with it. The
backend gets relief it wouldn't get from real clients, and the failure looks smaller than it is:
latency goes up, but errors and load stay low, and a congestive collapse (retry storm, metastable
overload) may never happen. Use closed loop when the story is about a bounded population: a fixed
set of workers, a client that waits its turn, a smoke test, a healthy steady state.

## Open loop (parallel): what request-router-metadata-app does

(An internal GitLab demo: where it is, `cre-failure-injection.md`.)

Requests are issued at a **fixed rate, whatever the latency**, the way independent clients on the
internet arrive. Each user's task starts the request in its own greenlet and returns at once, so
the next one is scheduled on time even while earlier ones are still waiting. When the backend slows
down the offered rate stays flat, in-flight requests pile up, timeouts fire, and availability falls
toward `capacity / offered rate`, which is the failure the audience should see.

```python
import os
import time

import gevent
import gevent.pool
from locust import FastHttpUser, between, constant_throughput, task

from cre_emf import emit       # the client metrics; cre_emf.py ships in the base image

LOAD_MODEL = os.environ.get("LOAD_MODEL", "closed")          # open | closed
TASK_RATE = float(os.environ.get("TASK_RATE", "1.0"))        # requests/s per user (open)
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "5"))
OPEN_CONCURRENCY = int(os.environ.get("OPEN_CONCURRENCY", "10"))   # in-flight cap per user
NAME = "POST /work"


def make_body():
    return {"id": time.time_ns()}             # the demo's own request body


class WorkUser(FastHttpUser):
    wait_time = constant_throughput(TASK_RATE) if LOAD_MODEL == "open" else between(0.5, 1.5)
    concurrency = OPEN_CONCURRENCY            # FastHttpUser's connection pool per user
    network_timeout = connection_timeout = REQUEST_TIMEOUT

    def on_start(self):
        self._inflight = gevent.pool.Pool(OPEN_CONCURRENCY)

    def on_stop(self):
        self._inflight.kill(block=False)      # don't leave requests running past the test

    def _post(self, body):
        start, counted = time.monotonic(), False
        try:
            with gevent.Timeout(REQUEST_TIMEOUT):           # the client's SLA, enforced
                with self.client.post("/work", json=body, catch_response=True, name=NAME) as r:
                    # A send timeout or a refused connection arrives here as status 0.
                    ok = 200 <= r.status_code < 400
                    emit(ok, (time.monotonic() - start) * 1000, NAME)
                    counted = True
                    if not ok:
                        r.failure(f"status {r.status_code}")
        except gevent.Timeout:
            # Only a timeout while the body is read gets here, and Locust records nothing for it.
            if not counted:
                emit(False, (time.monotonic() - start) * 1000, NAME)

    @task
    def work(self):
        if LOAD_MODEL == "open":
            self._inflight.spawn(self._post, make_body())   # fire and forget
        else:
            self._post(make_body())
```

What makes it work, and what breaks without each piece:

- **`constant_throughput(TASK_RATE)`** fixes how often each user's task runs; the task returning at
  once is what keeps that cadence when responses are slow. Offered rate = users × `TASK_RATE`.
- **`FastHttpUser`** (geventhttpclient), not `HttpUser` (requests): a fraction of the CPU and memory
  per connection, so one worker holds thousands of concurrent connections when the backend stalls.
- **A bounded pool** (`gevent.pool.Pool`): without it, in-flight greenlets grow without limit when
  completions fall behind, the gevent hub starves, timeouts stop firing, and the worker reports
  300-second "latencies" and runs out of memory. Size it at about `TASK_RATE × REQUEST_TIMEOUT`
  with 2× headroom (1 req/s × 5 s → 10). A full pool blocks `spawn` briefly, which sheds load only
  when the backend is already dead.
- **A hard per-request timeout** (`gevent.Timeout`): a real client gives up at its SLA and counts a
  failure. `network_timeout` alone doesn't abort a server that keeps the connection alive, and a
  request held open while the backend retries would hold a pool slot.
- **A high open-file limit**: connections climb when the backend slows; `LoadGenerator`'s
  `nofileUlimit` (for example 65536).
- **Enough generators**: spread the rate with `workers` and `processesPerTask` (one process per
  core); watch the workers' CPU, because a saturated generator also lowers the rate it offers.
  Size it by measuring, not by a rule of thumb: run one worker task, raise the rate until its CPU
  nears 80% (or Locust warns about CPU), and divide the target rate by that rate per task, with 2×
  headroom.
- **`requestTimeoutSeconds`** (default 5) sets `REQUEST_TIMEOUT`, which the base locustfile and the
  open-loop one above read: set it on `LoadGenerator`, not with `addEnvironment`.
- **Count what the client sees**: success only for 2xx/3xx, and one `emit` per request on every
  path. A `workloadDirectory` locustfile **replaces** the base one, so it emits nothing unless it
  calls `cre_emf.emit`: no `ClientSuccess`/`ClientError`, an empty availability graph, and a
  `ClientAvailabilityAlarm` (and FIS stop condition) that can't fire. Locust turns a timeout while
  sending into a response with status 0 (a failure in its stats); a timeout while the body is
  read reaches only the `except`, which is why it emits there.

Keep `LOAD_MODEL` as an environment switch (set with `loadGenerator.addEnvironment`) so the same
image runs both, and say in the design which one the story uses.

**Run it before deploying.** `npx projen build` checks that the image's Python parses; a page or
route clash, a missing package or a crash at start only shows when it runs. With Docker, after a
build (`<hash>` is in `assets/containers/manifest.json`):

```sh
docker load -i assets/containers/<hash>.tar.gz
docker run --rm -p 8089:8089 -e TARGET_URL=https://example.com -e LOCUST_WEB_UI=true \
  -e LOCUST_WEB_BASE_PATH=/locust -e AWS_DEFAULT_REGION=us-east-2 cdk-asset:<hash>
```

Then open `http://localhost:8089/locust/` and each page. `AWS_DEFAULT_REGION` is needed for a
module-scope boto3 client: boto3 reads only that variable, never `AWS_REGION` (with only
`AWS_REGION` set, boto3 1.43 raises `NoRegionError`). Fargate sets it in the task.
The container has no AWS credentials, so a page that reads AWS (an `/inject` page's status read)
answers 500 (`NoCredentialsError`) and stays on "loading...": expected, not a bug in the handler.
To exercise those reads, pass read-only credentials for the demo's account:
`aws configure export-credentials --profile <p> --format env-no-export >/tmp/creds.env`, then add
`--env-file /tmp/creds.env` to the `docker run`, and delete the file after. Never bake credentials
into the image or commit them; a page that arms a failure then acts on the real account, so POST
only when the user asks.

**Packages.** The image has `locust`, `requests` and `boto3`. Put any other package in the
overlay's `requirements-extra.txt`, never a `requirements.txt` (that replaces the base one, the
Locust pin with it, and warns at synth).

## Running a test

- **Start and stop with Locust's own API**, not by scaling the worker service: `POST /swarm`
  (`user_count`, `spawn_rate`) starts a run, `GET /stop` stops it. Workers that reconnect don't
  restart a stopped swarm. From an agent: the `locust-cfs` skill
  (`.kiro/skills/locust-cfs/scripts/lc.py POST /swarm 'user_count=40&spawn_rate=5'`, from the
  project root).
- **Choosing user classes** (`user_classes` repeated per class in `/swarm`) needs
  `LoadGenerator`'s `classPicker: true` (Locust's `--class-picker`). Without it Locust ignores
  `user_classes` and runs every class in the locustfile at its weight. To change the classes,
  stop first.
- **Ramp slowly into a cold system** (spawn rate around 10 users/s): thousands of users arriving at
  once into empty caches is a thundering herd that collapses the backend during warm-up, a false
  failure.
- **Measure at the plateau**: the ramp to 4800 users at 10/s takes 8 minutes. Read steady
  availability after the target user count is reached and has held, not mid-ramp.
- **Size the system under test for the healthy load first**: driving thousands of users at a fleet
  resting at its minimum makes failures that look like a bug. Scale the system to the intended
  ratio before calling a result a defect.
- **Reset between independent trials**: a system pushed into a metastable state can stay there;
  shed the load and wait until it is healthy before the next run.
- **An orchestrated run** (scale up, ramp, hold, arm, hold, disarm, shed load) belongs on a
  Locust UI page so it runs the same way every time: `cre-failure-injection.md`, "Operator
  controls in the Locust UI".

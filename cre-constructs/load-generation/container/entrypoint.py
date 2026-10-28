#!/usr/bin/env python3
"""Entry point for the generic Locust load generator.

The SAME image serves every LoadGenerator topology, chosen by env the construct injects:

  LOCUST_ROLE (default standalone)
    standalone  one task generates the load itself.
    controller  coordinator only (LoadGenerator `workers` > 0): runs no users, listens for worker
                RPC on 5557 and spreads the users across the connected workers.
    worker      generator: no web UI; connects to LOCUST_CONTROLLER_HOST:LOCUST_CONTROLLER_PORT
                (Cloud Map name of the controller task) and forks LOCUST_PROCESSES processes.
  LOCUST_WEB_UI=true    web UI on :8089, under LOCUST_WEB_BASE_PATH when set (the CloudFront Signer
                        edge route, e.g. /locust; the ALB health-checks it). Otherwise headless (a
                        headless controller waits for LOCUST_EXPECT_WORKERS workers).
  LOCUST_CLASS_PICKER=true
                        with the web UI: `--class-picker`, so the start form and /swarm's
                        user_classes choose the classes (otherwise every class runs).
  LOCUST_USERS / LOCUST_SPAWN_RATE
                        the run size when headless; the start form's defaults with the web UI.
  LOCUST_PROCESSES      `--processes N` for standalone and worker.

cre_pages.py (the base image's) serves LoadGenerator.addWebPage() pages and @route handlers on the
web UI; it is loaded next to the locustfile whenever it is present.

This process is replaced by locust (exec), so locust is PID 1 and gets the container's signals.
Exit 64 for an unknown LOCUST_ROLE or a worker with no LOCUST_CONTROLLER_HOST; 127 when locust
isn't installed (review 10c E9: the shell version's `set -u` and exec gave the same).
"""
import os
import sys


def locust_args(env, have_pages):
    # --host overrides the locustfile's own (normalised) host, so strip the trailing slash here too,
    # or "<api-gw-url>/" + "/path" still requests "//path".
    host = env.get("TARGET_URL", "").rstrip("/")
    role = env.get("LOCUST_ROLE") or "standalone"
    args = ["-f", "locustfile.py,cre_pages.py" if have_pages else "locustfile.py", "--host", host]
    if role == "worker":
        if not env.get("LOCUST_CONTROLLER_HOST"):
            raise ValueError("entrypoint: LOCUST_ROLE=worker needs LOCUST_CONTROLLER_HOST (the controller's Cloud Map name)")
        args += ["--worker", "--master-host", env["LOCUST_CONTROLLER_HOST"],
                 "--master-port", env.get("LOCUST_CONTROLLER_PORT") or "5557"]
        if env.get("LOCUST_PROCESSES"):
            args += ["--processes", env["LOCUST_PROCESSES"]]
        return args
    if role == "controller":
        args.append("--master")
    elif role == "standalone":
        if env.get("LOCUST_PROCESSES"):
            args += ["--processes", env["LOCUST_PROCESSES"]]
    else:
        raise ValueError(f"entrypoint: unknown LOCUST_ROLE '{role}' (standalone | controller | worker)")
    args += ["--users", env.get("LOCUST_USERS") or "10", "--spawn-rate", env.get("LOCUST_SPAWN_RATE") or "10"]
    if env.get("LOCUST_WEB_UI", "false") == "true":
        args += ["--web-port", "8089"]
        if env.get("LOCUST_WEB_BASE_PATH"):
            args += ["--web-base-path", env["LOCUST_WEB_BASE_PATH"]]
        if env.get("LOCUST_CLASS_PICKER") == "true":
            args.append("--class-picker")
    else:
        args.append("--headless")
        if role == "controller" and env.get("LOCUST_EXPECT_WORKERS"):
            args += ["--expect-workers", env["LOCUST_EXPECT_WORKERS"]]
    return args


def main():
    try:
        args = locust_args(os.environ, os.path.isfile("cre_pages.py"))
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(64)
    sys.stdout.flush()
    try:
        os.execvp("locust", ["locust", *args])
    except FileNotFoundError:
        print("entrypoint: locust not found on PATH (is it in requirements.txt?)", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()

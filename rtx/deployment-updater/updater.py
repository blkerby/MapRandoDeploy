"""Follow GitHub Deployment requests for a configured Kubernetes target."""

import argparse
import logging
import time
from pathlib import Path

import requests
import urllib3
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--environment-url", required=True)
    parser.add_argument("--poll-seconds", type=int, required=True)
    parser.add_argument("--active-states", nargs="+", required=True)
    return parser.parse_args()


class GitHub:
    def __init__(self, token_file: Path, settings: argparse.Namespace):
        self.settings = settings
        self.poll_seconds = settings.poll_seconds
        self.poll_intervals = []
        self.url = f"https://api.github.com/repos/{settings.repository}"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token_file.read_text().strip()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def update_poll_interval(self, response: requests.Response):
        interval = response.headers.get("X-Poll-Interval")
        if interval is not None:
            self.poll_intervals.append(interval)
            self.poll_seconds = max(self.poll_seconds, int(interval))

    def get(self, path: str, params: dict):
        response = self.session.get(f"{self.url}/{path}", params=params, timeout=30)
        self.update_poll_interval(response)
        response.raise_for_status()
        return response.json()

    def latest_deployment(self):
        deployments = self.get("deployments", {
            "environment": self.settings.environment,
            "task": self.settings.task,
            "per_page": 1,
        })
        return deployments[0] if deployments else None

    def latest_status(self, request: dict):
        statuses = self.get(f"deployments/{request['id']}/statuses", {"per_page": 1})
        return statuses[0] if statuses else None

    def set_status(self, request: dict, state: str, description: str):
        response = self.session.post(
            f"{self.url}/deployments/{request['id']}/statuses",
            json={
                "state": state,
                "description": description,
                "environment_url": self.settings.environment_url,
                "auto_inactive": state == "success",
            },
            timeout=30,
        )
        self.update_poll_interval(response)
        response.raise_for_status()
        logging.info("Request %s: %s — %s", request["id"], state, description)


def application_container(deployment, container_name: str):
    for container in deployment.spec.template.spec.containers:
        if container.name == container_name:
            return container
    raise RolloutFailure(f"Container {container_name} does not exist")


def rollout_complete(deployment):
    status = deployment.status
    replicas = deployment.spec.replicas
    return (
        status.observed_generation is not None
        and status.observed_generation >= deployment.metadata.generation
        and status.replicas == replicas
        and status.updated_replicas == replicas
        and status.ready_replicas == replicas
        and status.available_replicas == replicas
    )


class RolloutFailure(Exception):
    pass


def follow_request(github: GitHub, apps: client.AppsV1Api, request: dict, status, settings: argparse.Namespace):
    image = f"{settings.image_repository}@{request['payload']['digest']}"
    deploy_config = request["payload"].get("deploy_config")
    if not isinstance(deploy_config, dict) or set(deploy_config) != {"args"}:
        raise RolloutFailure("Deployment payload must include deploy_config with exactly an args field")
    args = deploy_config["args"]
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise RolloutFailure("Deployment configuration args must be an array of strings")
    deployment = apps.read_namespaced_deployment(settings.deployment, settings.namespace, _request_timeout=30)
    container = application_container(deployment, settings.container)
    release_matches = container.image == image and (container.args or []) == args

    if release_matches and rollout_complete(deployment):
        github.set_status(request, "success", "Requested image and deployment configuration are deployed and healthy")
        return

    if status is None or status["state"] != "in_progress":
        if not release_matches:
            logging.info("Request %s: setting %s image and arguments for %s", request["id"], settings.container, image)
            apps.patch_namespaced_deployment(
                settings.deployment,
                settings.namespace,
                {
                    "metadata": {"resourceVersion": deployment.metadata.resource_version},
                    "spec": {"template": {"spec": {"containers": [{"name": settings.container, "image": image, "args": args}]}}},
                },
                _content_type="application/strategic-merge-patch+json",
                _request_timeout=30,
            )
        # Record progress after patching. If this write fails, the next poll sees
        # the requested image and arguments and continues without patching again.
        github.set_status(request, "in_progress", "Waiting for the Kubernetes rollout")
        return

    if not release_matches:
        raise RolloutFailure("Deployment image or arguments changed while this request was in progress")

    observed = deployment.status.observed_generation
    if observed is not None and observed >= deployment.metadata.generation:
        conditions = deployment.status.conditions
        if conditions is not None:
            for condition in conditions:
                if condition.type == "Progressing" and condition.status == "False" and condition.reason == "ProgressDeadlineExceeded":
                    raise RolloutFailure("Kubernetes rollout exceeded its progress deadline")


def reconcile(github: GitHub, apps: client.AppsV1Api, settings: argparse.Namespace):
    request = github.latest_deployment()
    if request is None:
        return
    status = github.latest_status(request)
    if status is not None and status["state"] not in settings.active_states:
        return

    try:
        follow_request(github, apps, request, status, settings)
    except RolloutFailure as error:
        github.set_status(request, "failure", str(error))


def main():
    settings = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.load_incluster_config()
    apps = client.AppsV1Api()
    github = GitHub(Path("/var/run/secrets/github/token"), settings)
    while True:
        github.poll_seconds = settings.poll_seconds
        github.poll_intervals.clear()
        try:
            reconcile(github, apps, settings)
        except (requests.RequestException, ApiException, urllib3.exceptions.HTTPError, OSError) as error:
            logging.warning("API operation failed; will retry on the next poll: %s", error)
        logging.info(
            "Poll complete: X-Poll-Interval=%s; next poll in %s seconds",
            github.poll_intervals if github.poll_intervals else "not provided",
            github.poll_seconds,
        )
        time.sleep(github.poll_seconds)


if __name__ == "__main__":
    main()

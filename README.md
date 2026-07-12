# CI Dashboard — Kubernetes deploy

Flask app aggregating GitHub Actions status across projects. This repo builds
and pushes the container image; the running config lives in the separate
[`ci-dashboard-chart`](https://github.com/IPpetrov/ci-dashboard-chart) repo,
deployed via Helm + ArgoCD, with Prometheus/Grafana for observability.

This is a **separate codebase from the Lambda deployment** in
[`ci-dashboard`](https://github.com/IPpetrov/ci-dashboard). Same app, two
entrypoints — this one runs under gunicorn as a normal HTTP server, the other
runs as a Lambda handler. **Do not merge these repos or copy `main.py`
between them without checking the entrypoint.**

## Architecture

```
git push (this repo)
  -> GitHub Actions: test, build image, push to private ECR
  -> checks out ci-dashboard-chart, bumps values.yaml image.tag to the
     commit SHA, commits, pushes
  -> ArgoCD (running in the kind cluster) polls ci-dashboard-chart,
     detects the change, runs Helm, rolls out new pods
  -> Prometheus scrapes /metrics on each pod via a ServiceMonitor;
     Grafana visualizes both infra and app-level metrics
```

## Observability

The app exposes Prometheus-format metrics at `/metrics` via
`prometheus-flask-exporter` (per-route request count, latency histogram).
Scraped every 15s by Prometheus through a `ServiceMonitor` defined in
`ci-dashboard-chart`. Visualized in Grafana at `http://localhost:30300`
(`admin` / `prom-operator` by default — change this if the cluster is ever
more than local/throwaway).

Infra-level metrics (CPU/memory per pod) come for free from
`kube-prometheus-stack`'s bundled dashboards — no app changes needed for
those.

## One-time setup (already done, documented for reproducibility)

### AWS / ECR
- Private ECR repo: `ci-dashboard-k8s` in `eu-central-1`
  - **Separate from** the Lambda deploy's ECR repo — do not reuse.
- IAM user `GitHubECRaccess` needs the **`AmazonEC2ContainerRegistryFullAccess`**
  managed policy (this is private ECR, despite the legacy "EC2 Container
  Registry" name). `AmazonElasticContainerRegistryPublicFullAccess` is a
  *different* service (ECR Public / the gallery) and does NOT grant
  `ecr:GetAuthorizationToken` for private repos.
- A second, minimal IAM user, `ECRPullRefresher`, with
  `AmazonEC2ContainerRegistryReadOnly` — used only by the in-cluster
  CronJob that refreshes the pull secret (see below). Deliberately
  separate credentials from `GitHubECRaccess` so a leak in either place
  has a smaller blast radius.

### GitHub Actions secrets (repo Settings → Secrets and variables → Actions)
| Secret | Value | Notes |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | — | IAM user `GitHubECRaccess` |
| `AWS_SECRET_ACCESS_KEY` | — | same user |
| `AWS_REGION` | `eu-central-1` | |
| `ECR_REPO_K8S` | `992382571377.dkr.ecr.eu-central-1.amazonaws.com/ci-dashboard-k8s` | **not** the Lambda repo's URI |
| `CHART_REPO_PAT` | — | Fine-grained GitHub token, scoped to `ci-dashboard-chart` only, Contents: Read and write. Regenerate before it expires. |

### Kubernetes — ECR pull secret (auto-refreshed)
The kind cluster needs its own credentials to pull the private image,
separate from the AWS creds GitHub Actions uses. The token is short-lived
(~12h), so a CronJob (`ecr-refresher-cronjob.yaml` + RBAC in
`ci-dashboard-chart`) re-runs the refresh every 6 hours automatically —
no manual intervention needed under normal operation.

Manual refresh, only if needed (e.g. right after a full cluster recreate,
before the CronJob's first scheduled run):
```bash
kubectl create secret docker-registry ecr-pull-secret \
  --docker-server=992382571377.dkr.ecr.eu-central-1.amazonaws.com \
  --docker-username=AWS \
  --docker-password=$(aws ecr get-login-password --region eu-central-1) \
  --namespace=default

kubectl create secret generic aws-ecr-refresher-creds \
  --from-literal=AWS_ACCESS_KEY_ID=<ECRPullRefresher access key> \
  --from-literal=AWS_SECRET_ACCESS_KEY=<ECRPullRefresher secret key> \
  --namespace=default
```

## Local development / re-creating the cluster from scratch

```bash
kind create cluster --name devops-lab --config kind-config.yml
docker build -t ci-dashboard:local .
kind load docker-image ci-dashboard:local --name devops-lab

kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml --server-side
kubectl -n argocd wait --for=condition=available --timeout=180s deployment/argocd-server
kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "NodePort", "ports": [{"name":"https","port":443,"targetPort":8080,"nodePort":30443},{"name":"http","port":80,"targetPort":8080}]}}'

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
kubectl create namespace monitoring
helm install monitoring prometheus-community/kube-prometheus-stack --namespace monitoring
kubectl patch svc monitoring-grafana -n monitoring -p '{"spec": {"type": "NodePort", "ports": [{"port":80,"targetPort":3000,"nodePort":30300}]}}'

# (create the ECR pull secrets above, then:)
cd ../ci-dashboard-chart
kubectl apply -f ecr-refresher-rbac.yaml
kubectl apply -f ecr-refresher-cronjob.yaml
kubectl apply -f argocd-app.yaml
```

`kind-config.yml` maps three ports to the host, so nothing above needs
`kubectl port-forward` to be reachable:
- `30080` → dashboard app
- `8081` → `30443` (ArgoCD UI, HTTPS)
- `30300` → Grafana

Note: `kubectl apply -f install.yaml` uses `--server-side` — plain
`kubectl apply` fails on this manifest with `annotations: Too long` because
the `ApplicationSet` CRD's schema exceeds the 256KB `last-applied-configuration`
annotation limit. Server-side apply doesn't hit this limit.

## Known limitations / follow-ups

- **ArgoCD polls Git every ~3 minutes by default** — a `git push` to
  `ci-dashboard-chart` isn't instant. A GitHub webhook to ArgoCD would make
  this immediate; not yet set up.
- **`ci-dashboard-chart`'s `values.yaml` is auto-edited by CI** (image
  repository/tag) on every push here. Manual edits to that file should
  `git pull` first — this repo has `pull.rebase` unset by default; running
  `git config pull.rebase false` avoids the "diverged branches" prompt.
- **NetworkPolicy enforcement depends on the CNI.** kind's default CNI
  (`kindnet`) has limited/no NetworkPolicy enforcement depending on
  version — the policy is defined and correct, but may not actually be
  enforced locally. Verified functionally correct against real traffic
  (health check + GitHub API egress both still work with the policy
  applied), but real enforcement should be re-verified on a cluster with a
  policy-supporting CNI (Calico, Cilium, or any managed cloud cluster).

## Incident log

- **2026-07-11** — Accidentally pushed this repo's gunicorn-based code to
  the `ci-dashboard` (Lambda) repo's `main` branch, which triggered its
  deploy workflow and broke the live Lambda function (`lambda_handler` no
  longer existed in the code). Caught via the failing `/health` check step
  in that workflow, reverted with `git revert`, confirmed recovery via
  `curl .../health`. Root cause: `~/ci-dashboard` locally still had
  `origin` pointed at the old Lambda repo; assumed it was a fresh repo
  instead of checking `git remote -v` first. Fixed by repointing `origin`
  with `git remote set-url` to the correct new repo before any further
  pushes.

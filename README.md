# CI Dashboard — Kubernetes deploy

Flask app aggregating GitHub Actions status across projects. This repo builds
and pushes the container image; the running config lives in the separate
[`ci-dashboard-chart`](https://github.com/IPpetrov/ci-dashboard-chart) repo,
deployed via Helm + ArgoCD.

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
```

## One-time setup (already done, documented for reproducibility)

### AWS / ECR
- Private ECR repo: `ci-dashboard-k8s` in `eu-central-1`
  - **Separate from** the Lambda deploy's ECR repo — do not reuse.
- IAM user `GitHubECRaccess` needs the **`AmazonEC2ContainerRegistryFullAccess`**
  managed policy (this is private ECR, despite the legacy "EC2 Container
  Registry" name). `AmazonElasticContainerRegistryPublicFullAccess` is a
  *different* service (ECR Public / the gallery) and does NOT grant
  `ecr:GetAuthorizationToken` for private repos — cost us a failed run
  figuring that out.

### GitHub Actions secrets (repo Settings → Secrets and variables → Actions)
| Secret | Value | Notes |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | — | IAM user `GitHubECRaccess` |
| `AWS_SECRET_ACCESS_KEY` | — | same user |
| `AWS_REGION` | `eu-central-1` | |
| `ECR_REPO_K8S` | `992382571377.dkr.ecr.eu-central-1.amazonaws.com/ci-dashboard-k8s` | **not** the Lambda repo's URI |
| `CHART_REPO_PAT` | — | Fine-grained GitHub token, scoped to `ci-dashboard-chart` only, Contents: Read and write. Regenerate before it expires (check expiry date set at creation). |

### Kubernetes — ECR pull secret
The kind cluster needs its own credentials to pull the private image
(separate from the AWS creds GitHub Actions uses). This is a **short-lived
token (~12h)** and will need refreshing — see Known limitations below.

```bash
kubectl create secret docker-registry ecr-pull-secret \
  --docker-server=992382571377.dkr.ecr.eu-central-1.amazonaws.com \
  --docker-username=AWS \
  --docker-password=$(aws ecr get-login-password --region eu-central-1) \
  --namespace=default
```

Referenced in `ci-dashboard-chart/values.yaml` via `imagePullSecrets`.

## Local development / re-creating the cluster from scratch

```bash
kind create cluster --name devops-lab --config kind-config.yml
docker build -t ci-dashboard:local .
kind load docker-image ci-dashboard:local --name devops-lab
kubectl apply -f deployment.yml   # only for quick manual testing —
kubectl apply -f service.yml      # normal deploys go through Helm/ArgoCD, not these
```

`kind-config.yml` maps container port 30080 to the host so
`localhost:30080` works without `kubectl port-forward`.

## Known limitations / follow-ups

- **ECR pull secret expires in ~12h.** Currently manual re-run of the
  `kubectl create secret` command above. In a real cluster this would be
  automated (IRSA on EKS, or a CronJob re-running the token refresh). Worth
  fixing before leaving this running unattended for more than a day.
- **ArgoCD polls Git every ~3 minutes by default** — a `git push` to
  `ci-dashboard-chart` isn't instant. A GitHub webhook to ArgoCD would make
  this immediate; not yet set up.
- **`ci-dashboard-chart`'s `values.yaml` is auto-edited by CI** (image
  repository/tag) on every push here. Manual edits to that file should
  `git pull` first to avoid merge conflicts with the bot's commits.

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

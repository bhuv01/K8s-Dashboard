# KubeWatch — Kubernetes Cluster Status Dashboard

A real-time, read-only monitoring dashboard that runs as a single pod inside your cluster. Visual overview of nodes, pods, deployments, statefulsets, PVCs, jobs, and cronjobs — with live CPU/memory usage for every pod and node.

No Prometheus. No Grafana. No database. Just the K8s API + metrics-server.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  BROWSER                                                 │
│  ┌────────────────────────────────────────────────────┐  │
│  │  KubeWatch Dashboard (Single-Page App)             │  │
│  │  • Auto-refreshes every 30s                        │  │
│  │  • All rendering client-side (vanilla JS)          │  │
│  └──────────────────────┬─────────────────────────────┘  │
└─────────────────────────┼────────────────────────────────┘
                          │ HTTP :80 → :8080
┌─────────────────────────┼────────────────────────────────┐
│  KUBERNETES CLUSTER     │                                │
│                         ▼                                │
│  ┌───────────────────────────────────────────────┐      │
│  │  KubeWatch Pod (monitoring namespace)         │      │
│  │  Python 3.11 + Flask · Non-root user          │      │
│  │                                               │      │
│  │  GET /             → Dashboard UI             │      │
│  │  GET /api/status   → Cluster data JSON        │      │
│  │  GET /api/namespaces → Namespace list         │      │
│  │  GET /healthz      → Health probe             │      │
│  └──────────────┬────────────────────────────────┘      │
│                 │ ServiceAccount (read-only)              │
│                 ▼                                         │
│  ┌───────────────────────────────────────────────┐      │
│  │  Kubernetes API Server                        │      │
│  │                                               │      │
│  │  CoreV1Api ─── pods, nodes, PVCs, namespaces  │      │
│  │  AppsV1Api ─── deployments, statefulsets       │      │
│  │  BatchV1Api ── jobs, cronjobs                  │      │
│  └──────────────┬────────────────────────────────┘      │
│                 │                                         │
│  ┌──────────────▼────────────────────────────────┐      │
│  │  metrics-server (kube-system)                 │      │
│  │  metrics.k8s.io → pod & node CPU/memory       │      │
│  └───────────────────────────────────────────────┘      │
│                                                          │
│  RBAC: get, list, watch only — zero write access         │
└──────────────────────────────────────────────────────────┘
```

---

## Features

- **Auto Namespace Discovery** — Finds all namespaces automatically (kube-system, default, etc.)
- **Namespace Dropdown** — Searchable multi-select to pick which namespaces to monitor
- **Pod CPU & Memory Bars** — Live usage vs limits/requests with color-coded thresholds
- **Node Health Cards** — CPU/memory as % of allocatable capacity
- **Six Views** — Nodes · Pods · Deployments · StatefulSets · PVCs · Jobs/CronJobs
- **Inline Pod Logs** — Click any unhealthy pod to see last 50 log lines
- **Issues Only Mode** — One click to filter down to problems only
- **Name Search** — Filter any view by resource name
- **Auto-Refresh** — Every 30 seconds + manual refresh button
- **Read-Only** — Cannot modify anything in your cluster
- **Single Pod** — No agents, no sidecars, no external dependencies

---

## Prerequisites

1. Kubernetes cluster (v1.24+)
2. **metrics-server** installed and running:
   ```bash
   # Verify
   kubectl top nodes

   # Install if missing
   kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
   ```
3. Docker and a container registry (ECR, Docker Hub, etc.)

---

## File Structure

```
├── Dockerfile            # Python 3.11-slim, non-root user
├── requirements.txt      # kubernetes, flask, requests
├── status_server.py      # Backend + embedded frontend (single file)
├── status_rbac.yaml      # ServiceAccount + ClusterRole + ClusterRoleBinding
├── status_deploy.yaml    # Deployment + ClusterIP Service
└── README.md
```

---

## Installation

### 1. Create namespace

```bash
kubectl create namespace monitoring
```

### 2. Apply RBAC

```bash
kubectl apply -f status_rbac.yaml
```

This creates a read-only `ClusterRole` (`kubewatch-readonly`) with access to namespaces, nodes, pods, pods/log, PVCs, deployments, statefulsets, jobs, cronjobs, and metrics. Only `get`, `list`, `watch` verbs — no write permissions.

### 3. Build and push Docker image

```bash
docker build -t <Your Docker Registry>:dashboard-v1.0 .
docker push <Your Docker Registry>:dashboard-v1.0
```

### 4. Configure `status_deploy.yaml`

Before deploying, update these fields in `status_deploy.yaml`:

- **`image`** — Replace `<Your Docker Registry>:dashboard-v1.0` with your actual registry path.
- **`CLUSTER_NAME`** — Set your cluster's display name.
- **`imagePullSecrets`** — The manifest references a secret named `ecr-secret`. If you use ECR, create it:
  ```bash
  kubectl create secret docker-registry ecr-secret \
    --namespace monitoring \
    --docker-server=<your-account>.dkr.ecr.<region>.amazonaws.com \
    --docker-username=AWS \
    --docker-password=$(aws ecr get-login-password --region <region>)
  ```
  If you use Docker Hub or a public registry, remove the `imagePullSecrets` block from the YAML.

### 5. Deploy

```bash
kubectl apply -f status_deploy.yaml
```

### 6. Access the dashboard

```bash
kubectl port-forward svc/k8s-status -n monitoring 8080:80
```

Open **http://localhost:8080**

For production access, use an Ingress or change the Service type in `status_deploy.yaml` to `NodePort` or `LoadBalancer`.

---

## Configuration

Environment variables in `status_deploy.yaml`:

| Variable | Default (code) | Set in deploy.yaml | Description |
|---|---|---|---|
| `CLUSTER_NAME` | `unknown-cluster` | `Cluster Name` | Display name shown in the dashboard header |
| `CPU_THRESHOLD` | `85` | `85` | Node CPU % above which a HighCPU warning is raised |
| `MEM_THRESHOLD_MI` | `40000` | `3000` | Node memory (MiB) above which a HighMemory warning is raised |
| `POD_RESTART_THRESHOLD` | `3` | `3` | Pod restart count above which the pod is flagged critical |
| `STATUS_PORT` | `8080` | `8080` | Port the Flask server listens on |
| `LOG_LEVEL` | `INFO` | `INFO` | Python logging level (DEBUG, INFO, WARNING, ERROR) |

> **Note:** `MEM_THRESHOLD_MI` defaults to `40000` in code but is set to `3000` in the deploy manifest. Adjust this value based on your node sizes.

Namespaces are discovered automatically from the cluster — no namespace configuration needed.

---

## Usage

**Namespace Selection** — Click the dropdown in the filter bar. Search and check/uncheck namespaces. Selecting "All Namespaces" monitors everything including kube-system. Data refreshes immediately on selection change.

**Switching Views** — Use the sidebar icons on the left to switch between Nodes, Pods, Deployments, StatefulSets, PVCs, and Jobs/CronJobs.

**Pod Metrics** — Each pod row shows CPU and Memory progress bars. The bar fills relative to the pod's resource limit (or request if no limit is set). Colors: green (< 70%), amber (70–85%), red (> 85%). Text below shows exact values like `45m / 200m` (millicores used / limit).

**Pod Logs** — Pods with warnings or errors show a "▶ logs" indicator. Click the row to expand the last 50 log lines inline. Click again to collapse.

**Issues Only** — Click "⚠ Issues Only" in the top bar to hide all healthy resources. Only warning and critical items remain visible. Click "✕ Show All" to reset.

**Search** — Type in the search box to filter any view by resource name in real time.

---

## RBAC Details

The `status_rbac.yaml` creates three resources:

- **ServiceAccount** `status-sa` in the `monitoring` namespace
- **ClusterRole** `kubewatch-readonly` with read-only rules:
  - `namespaces` — for dynamic namespace discovery
  - `nodes` — node health and info
  - `pods`, `pods/log` — pod status and log tailing
  - `persistentvolumeclaims` — PVC status
  - `deployments`, `statefulsets` — replica counts
  - `jobs`, `cronjobs` — job status and schedules
  - `metrics.k8s.io` nodes and pods — CPU/memory from metrics-server
- **ClusterRoleBinding** `kubewatch-readonly-binding` — binds the role to the service account

All rules use only `get`, `list`, `watch` verbs. No create, update, patch, or delete.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Pod won't start | Check logs: `kubectl logs -n monitoring -l app=k8s-status` |
| ImagePullBackOff | Verify `imagePullSecrets` and registry path in `status_deploy.yaml` |
| Metrics unavailable | Verify metrics-server: `kubectl top nodes` |
| Namespace dropdown empty | Check RBAC: `kubectl auth can-i list namespaces --as=system:serviceaccount:monitoring:status-sa` |
| Pod CPU/RAM shows N/A | Pod has no limits/requests defined, or metrics-server hasn't scraped it yet |
| 403 errors in logs | RBAC not applied: `kubectl apply -f status_rbac.yaml` |
| Dashboard loads but no data | Check network policies aren't blocking pod → API server traffic |

---

## Resource Footprint

| | Request | Limit |
|---|---|---|
| CPU | 50m | 200m |
| Memory | 64Mi | 256Mi |

Single replica · Stateless · No persistent storage · Restarts safely anytime

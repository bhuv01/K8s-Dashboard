import os
import logging
import datetime
from flask import Flask, jsonify, render_template_string, request
from kubernetes import client, config
from kubernetes.client.rest import ApiException

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(funcName)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("k8s-status")

# ─────────────────────────────────────────────
# KUBERNETES INIT
# ─────────────────────────────────────────────
def init_k8s():
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster config.")
    except config.ConfigException:
        logger.warning("Falling back to kubeconfig.")
        config.load_kube_config()
    return (
        client.CoreV1Api(),
        client.AppsV1Api(),
        client.CustomObjectsApi(),
        client.BatchV1Api(),
    )

v1, apps_v1, custom_api, batch_v1 = init_k8s()

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
CLUSTER_NAME          = os.getenv("CLUSTER_NAME", "unknown-cluster")
CPU_THRESHOLD         = float(os.getenv("CPU_THRESHOLD", 85))
MEM_THRESHOLD_MI      = float(os.getenv("MEM_THRESHOLD_MI", 40000))
POD_RESTART_THRESHOLD = int(os.getenv("POD_RESTART_THRESHOLD", 3))

app = Flask(__name__)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)

def safe_list(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs).items
    except ApiException as e:
        logger.error("K8s API error: %s", e.reason)
        return []

def _age(ts):
    if not ts:
        return "unknown"
    delta = utc_now() - ts
    s = int(delta.total_seconds())
    if s < 60:    return f"{s}s"
    if s < 3600:  return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h"
    return f"{s // 86400}d"

def parse_cpu(cpu_str):
    """Parse K8s CPU string to millicores (int)."""
    if not cpu_str:
        return 0
    cpu_str = str(cpu_str)
    if cpu_str.endswith("n"):
        return int(cpu_str[:-1]) // 1_000_000
    if cpu_str.endswith("u"):
        return int(cpu_str[:-1]) // 1_000
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    try:
        return int(float(cpu_str) * 1000)
    except:
        return 0

def parse_memory_mi(mem_str):
    """Parse K8s memory string to MiB (float)."""
    if not mem_str:
        return 0.0
    mem_str = str(mem_str)
    if mem_str.endswith("Ki"):
        return round(int(mem_str[:-2]) / 1024, 1)
    if mem_str.endswith("Mi"):
        return round(float(mem_str[:-2]), 1)
    if mem_str.endswith("Gi"):
        return round(float(mem_str[:-2]) * 1024, 1)
    if mem_str.endswith("Ti"):
        return round(float(mem_str[:-2]) * 1024 * 1024, 1)
    if mem_str.endswith("K") or mem_str.endswith("k"):
        return round(int(mem_str[:-1]) / 1024, 1)
    if mem_str.endswith("M"):
        return round(float(mem_str[:-1]), 1)
    if mem_str.endswith("G"):
        return round(float(mem_str[:-1]) * 1024, 1)
    try:
        # Raw bytes
        return round(float(mem_str) / (1024 * 1024), 1)
    except:
        return 0.0

# ─────────────────────────────────────────────
# DYNAMIC NAMESPACE DISCOVERY
# ─────────────────────────────────────────────
def discover_namespaces():
    """Discover all namespaces in the cluster."""
    try:
        ns_list = v1.list_namespace()
        return sorted([ns.metadata.name for ns in ns_list.items])
    except ApiException as e:
        logger.error("Cannot list namespaces: %s", e.reason)
        # Fallback to env var
        fallback = os.getenv("WATCH_NAMESPACES", "default")
        return [ns.strip() for ns in fallback.split(",") if ns.strip()]

# ─────────────────────────────────────────────
# DATA COLLECTORS
# ─────────────────────────────────────────────
def collect_nodes():
    nodes = []
    try:
        items = v1.list_node().items
    except ApiException:
        return []

    metrics_map = {}
    try:
        metrics = custom_api.list_cluster_custom_object(
            group="metrics.k8s.io", version="v1beta1", plural="nodes"
        )
        for m in metrics.get("items", []):
            name = m["metadata"]["name"]
            cpu_m = parse_cpu(m["usage"]["cpu"])
            mem_mi = parse_memory_mi(m["usage"]["memory"])
            metrics_map[name] = {"cpu_m": cpu_m, "memory_mi": mem_mi}
    except Exception as e:
        logger.warning("Node metrics unavailable: %s", e)

    for node in items:
        name       = node.metadata.name
        conditions = {c.type: c.status for c in node.status.conditions}
        ready      = conditions.get("Ready") == "True"
        issues     = []

        for ctype in ("DiskPressure", "MemoryPressure", "PIDPressure"):
            if conditions.get(ctype) == "True":
                issues.append(ctype)

        # Allocatable resources
        allocatable = node.status.allocatable or {}
        alloc_cpu_m = parse_cpu(allocatable.get("cpu", "0"))
        alloc_mem_mi = parse_memory_mi(allocatable.get("memory", "0"))

        m = metrics_map.get(name, {})
        cpu_used_m = m.get("cpu_m", None)
        mem_used_mi = m.get("memory_mi", None)

        cpu_pct = None
        mem_pct = None
        if cpu_used_m is not None and alloc_cpu_m > 0:
            cpu_pct = round((cpu_used_m / alloc_cpu_m) * 100, 1)
        if mem_used_mi is not None and alloc_mem_mi > 0:
            mem_pct = round((mem_used_mi / alloc_mem_mi) * 100, 1)

        if cpu_pct and cpu_pct > CPU_THRESHOLD:
            issues.append(f"HighCPU ({cpu_pct}%)")
        if mem_used_mi and mem_used_mi > MEM_THRESHOLD_MI:
            issues.append(f"HighMemory ({mem_used_mi}Mi)")

        roles = [
            k.replace("node-role.kubernetes.io/", "")
            for k in (node.metadata.labels or {})
            if k.startswith("node-role.kubernetes.io/")
        ] or ["worker"]

        nodes.append({
            "name":         name,
            "status":       "healthy" if ready and not issues else "warning" if ready else "critical",
            "ready":        ready,
            "roles":        roles,
            "issues":       issues,
            "cpu_pct":      cpu_pct,
            "cpu_used_m":   cpu_used_m,
            "alloc_cpu_m":  alloc_cpu_m,
            "mem_pct":      mem_pct,
            "mem_used_mi":  mem_used_mi,
            "alloc_mem_mi": alloc_mem_mi,
            "version":      node.status.node_info.kubelet_version,
            "os":           node.status.node_info.os_image,
            "arch":         node.status.node_info.architecture,
        })

    return nodes


def _get_pod_logs(namespace, pod_name, tail=50):
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=tail,
        )
        return logs or ""
    except ApiException as e:
        logger.warning("Could not fetch logs for %s/%s: %s", namespace, pod_name, e.reason)
        return f"(Unable to fetch logs: {e.reason})"


def _get_pod_metrics(namespaces):
    """Fetch pod metrics for given namespaces. Returns dict: {ns: {pod_name: {cpu_m, mem_mi}}}"""
    pod_metrics = {}
    for ns in namespaces:
        pod_metrics[ns] = {}
        try:
            metrics = custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io", version="v1beta1",
                namespace=ns, plural="pods"
            )
            for m in metrics.get("items", []):
                pod_name = m["metadata"]["name"]
                total_cpu_m = 0
                total_mem_mi = 0.0
                for container in m.get("containers", []):
                    total_cpu_m += parse_cpu(container["usage"].get("cpu", "0"))
                    total_mem_mi += parse_memory_mi(container["usage"].get("memory", "0"))
                pod_metrics[ns][pod_name] = {
                    "cpu_m": total_cpu_m,
                    "mem_mi": round(total_mem_mi, 1),
                }
        except ApiException as e:
            logger.warning("Pod metrics unavailable for ns=%s: %s", ns, e.reason)
        except Exception as e:
            logger.warning("Pod metrics error for ns=%s: %s", ns, e)
    return pod_metrics


def _get_pod_resource_requests(pod):
    """Sum resource requests/limits across all containers in a pod spec."""
    total_req_cpu_m = 0
    total_req_mem_mi = 0.0
    total_lim_cpu_m = 0
    total_lim_mem_mi = 0.0
    for c in pod.spec.containers or []:
        res = c.resources or client.V1ResourceRequirements()
        requests = res.requests or {}
        limits = res.limits or {}
        total_req_cpu_m += parse_cpu(requests.get("cpu", "0"))
        total_req_mem_mi += parse_memory_mi(requests.get("memory", "0"))
        total_lim_cpu_m += parse_cpu(limits.get("cpu", "0"))
        total_lim_mem_mi += parse_memory_mi(limits.get("memory", "0"))
    return {
        "req_cpu_m": total_req_cpu_m,
        "req_mem_mi": round(total_req_mem_mi, 1),
        "lim_cpu_m": total_lim_cpu_m,
        "lim_mem_mi": round(total_lim_mem_mi, 1),
    }


def collect_pods(namespaces):
    result = {}
    pod_metrics = _get_pod_metrics(namespaces)

    for ns in namespaces:
        pods = []
        for pod in safe_list(v1.list_namespaced_pod, namespace=ns):
            phase    = pod.status.phase or "Unknown"
            issues   = []
            restarts = 0

            for cs in pod.status.container_statuses or []:
                restarts = max(restarts, cs.restart_count)
                if cs.restart_count > POD_RESTART_THRESHOLD:
                    issues.append(f"Restarts: {cs.restart_count}")
                if cs.state.waiting and cs.state.waiting.reason in (
                    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "OOMKilled"
                ):
                    issues.append(cs.state.waiting.reason)

            if pod.metadata.deletion_timestamp:
                stuck = (utc_now() - pod.metadata.deletion_timestamp).total_seconds()
                phase = "Terminating"
                if stuck > 120:
                    issues.append(f"StuckTerminating ({int(stuck)}s)")

            status = "healthy"
            if issues:
                status = "critical"
            elif phase not in ("Running", "Succeeded"):
                status = "warning"

            logs = ""
            if status in ("critical", "warning"):
                logs = _get_pod_logs(ns, pod.metadata.name)

            # Resource requests/limits from pod spec
            res_info = _get_pod_resource_requests(pod)

            # Actual usage from metrics-server
            pm = pod_metrics.get(ns, {}).get(pod.metadata.name, {})
            cpu_used_m = pm.get("cpu_m", None)
            mem_used_mi = pm.get("mem_mi", None)

            # Calculate utilization percentages against requests
            cpu_pct_req = None
            mem_pct_req = None
            cpu_pct_lim = None
            mem_pct_lim = None
            if cpu_used_m is not None and res_info["req_cpu_m"] > 0:
                cpu_pct_req = round((cpu_used_m / res_info["req_cpu_m"]) * 100, 1)
            if mem_used_mi is not None and res_info["req_mem_mi"] > 0:
                mem_pct_req = round((mem_used_mi / res_info["req_mem_mi"]) * 100, 1)
            if cpu_used_m is not None and res_info["lim_cpu_m"] > 0:
                cpu_pct_lim = round((cpu_used_m / res_info["lim_cpu_m"]) * 100, 1)
            if mem_used_mi is not None and res_info["lim_mem_mi"] > 0:
                mem_pct_lim = round((mem_used_mi / res_info["lim_mem_mi"]) * 100, 1)

            pods.append({
                "name":        pod.metadata.name,
                "namespace":   ns,
                "phase":       phase,
                "status":      status,
                "restarts":    restarts,
                "issues":      issues,
                "node":        pod.spec.node_name or "unscheduled",
                "age":         _age(pod.metadata.creation_timestamp),
                "logs":        logs,
                "cpu_used_m":  cpu_used_m,
                "mem_used_mi": mem_used_mi,
                "req_cpu_m":   res_info["req_cpu_m"],
                "req_mem_mi":  res_info["req_mem_mi"],
                "lim_cpu_m":   res_info["lim_cpu_m"],
                "lim_mem_mi":  res_info["lim_mem_mi"],
                "cpu_pct_req": cpu_pct_req,
                "mem_pct_req": mem_pct_req,
                "cpu_pct_lim": cpu_pct_lim,
                "mem_pct_lim": mem_pct_lim,
            })

        result[ns] = sorted(pods, key=lambda p: (p["status"] != "critical", p["status"] != "warning", p["name"]))
    return result


def collect_deployments(namespaces):
    result = {}
    for ns in namespaces:
        deps = []
        for d in safe_list(apps_v1.list_namespaced_deployment, namespace=ns):
            desired   = d.spec.replicas or 0
            available = d.status.available_replicas or 0
            ready     = d.status.ready_replicas or 0
            updated   = d.status.updated_replicas or 0
            status    = "healthy" if available >= desired and desired > 0 else "critical" if available == 0 else "warning"
            deps.append({
                "name":      d.metadata.name,
                "namespace": ns,
                "desired":   desired,
                "available": available,
                "ready":     ready,
                "updated":   updated,
                "status":    status,
                "age":       _age(d.metadata.creation_timestamp),
                "image":     d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else "",
            })
        result[ns] = sorted(deps, key=lambda x: x["status"] != "critical")
    return result


def collect_statefulsets(namespaces):
    result = {}
    for ns in namespaces:
        sets = []
        for sts in safe_list(apps_v1.list_namespaced_stateful_set, namespace=ns):
            desired = sts.spec.replicas or 0
            ready   = sts.status.ready_replicas or 0
            status  = "healthy" if ready >= desired and desired > 0 else "critical" if ready == 0 else "warning"
            sets.append({
                "name":      sts.metadata.name,
                "namespace": ns,
                "desired":   desired,
                "ready":     ready,
                "status":    status,
                "age":       _age(sts.metadata.creation_timestamp),
            })
        result[ns] = sorted(sets, key=lambda x: x["status"] != "critical")
    return result


def collect_pvcs(namespaces):
    result = {}
    for ns in namespaces:
        pvcs = []
        for pvc in safe_list(v1.list_namespaced_persistent_volume_claim, namespace=ns):
            phase   = pvc.status.phase or "Unknown"
            status  = "healthy" if phase == "Bound" else "critical" if phase == "Lost" else "warning"
            storage = ""
            if pvc.spec.resources and pvc.spec.resources.requests:
                storage = pvc.spec.resources.requests.get("storage", "")
            pvcs.append({
                "name":          pvc.metadata.name,
                "namespace":     ns,
                "phase":         phase,
                "status":        status,
                "storage_class": pvc.spec.storage_class_name or "default",
                "storage":       storage,
                "age":           _age(pvc.metadata.creation_timestamp),
            })
        result[ns] = sorted(pvcs, key=lambda x: x["status"] != "critical")
    return result


def collect_jobs(namespaces):
    result = {}
    for ns in namespaces:
        jobs = []
        for job in safe_list(batch_v1.list_namespaced_job, namespace=ns):
            failed    = job.status.failed or 0
            succeeded = job.status.succeeded or 0
            active    = job.status.active or 0
            conditions = job.status.conditions or []
            is_failed = any(c.type == "Failed"   and c.status == "True" for c in conditions)
            is_done   = any(c.type == "Complete" and c.status == "True" for c in conditions)
            status    = "healthy" if is_done else "critical" if is_failed or failed > 0 else "warning" if active > 0 else "healthy"
            jobs.append({
                "name":      job.metadata.name,
                "namespace": ns,
                "active":    active,
                "succeeded": succeeded,
                "failed":    failed,
                "status":    status,
                "age":       _age(job.metadata.creation_timestamp),
            })
        result[ns] = sorted(jobs, key=lambda x: x["status"] != "critical")
    return result


def collect_cronjobs(namespaces):
    result = {}
    for ns in namespaces:
        cjs = []
        for cj in safe_list(batch_v1.list_namespaced_cron_job, namespace=ns):
            suspended = cj.spec.suspend or False
            last_run  = cj.status.last_schedule_time
            status    = "warning" if suspended else "healthy"
            cjs.append({
                "name":      cj.metadata.name,
                "namespace": ns,
                "schedule":  cj.spec.schedule,
                "suspended": suspended,
                "last_run":  last_run.strftime("%Y-%m-%d %H:%M:%S UTC") if last_run else "Never",
                "active":    len(cj.status.active or []),
                "status":    status,
            })
        result[ns] = cjs
    return result


# ─────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────
def build_summary(nodes, pods, deployments, statefulsets, pvcs, jobs):
    def count_status(items_dict):
        total, healthy, warning, critical = 0, 0, 0, 0
        for items in items_dict.values():
            for i in items:
                total += 1
                s = i.get("status", "healthy")
                if s == "healthy":   healthy += 1
                elif s == "warning": warning += 1
                else:                critical += 1
        return {"total": total, "healthy": healthy, "warning": warning, "critical": critical}

    node_stats = {"total": len(nodes), "healthy": 0, "warning": 0, "critical": 0}
    for n in nodes:
        node_stats[n["status"]] += 1

    return {
        "nodes":        node_stats,
        "pods":         count_status(pods),
        "deployments":  count_status(deployments),
        "statefulsets": count_status(statefulsets),
        "pvcs":         count_status(pvcs),
        "jobs":         count_status(jobs),
    }


# ─────────────────────────────────────────────
# API: LIST NAMESPACES
# ─────────────────────────────────────────────
@app.route("/api/namespaces")
def api_namespaces():
    return jsonify({"namespaces": discover_namespaces()})


# ─────────────────────────────────────────────
# API: MAIN STATUS
# ─────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    # Accept ?namespaces=ns1,ns2 or default to all
    ns_param = request.args.get("namespaces", "")
    if ns_param.strip():
        namespaces = [ns.strip() for ns in ns_param.split(",") if ns.strip()]
    else:
        namespaces = discover_namespaces()

    nodes        = collect_nodes()
    pods         = collect_pods(namespaces)
    deployments  = collect_deployments(namespaces)
    statefulsets = collect_statefulsets(namespaces)
    pvcs         = collect_pvcs(namespaces)
    jobs         = collect_jobs(namespaces)
    cronjobs     = collect_cronjobs(namespaces)
    summary      = build_summary(nodes, pods, deployments, statefulsets, pvcs, jobs)

    has_critical = (
        summary["nodes"]["critical"] > 0 or
        summary["pods"]["critical"] > 0 or
        summary["deployments"]["critical"] > 0 or
        summary["statefulsets"]["critical"] > 0
    )
    has_warning = any(v["warning"] > 0 for v in summary.values())
    overall     = "critical" if has_critical else "warning" if has_warning else "healthy"

    return jsonify({
        "cluster":      CLUSTER_NAME,
        "namespaces":   namespaces,
        "overall":      overall,
        "timestamp":    utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary":      summary,
        "nodes":        nodes,
        "pods":         pods,
        "deployments":  deployments,
        "statefulsets": statefulsets,
        "pvcs":         pvcs,
        "jobs":         jobs,
        "cronjobs":     cronjobs,
    })


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML,
        cluster_name=CLUSTER_NAME,
    )


# ─────────────────────────────────────────────
# DASHBOARD HTML v3 — No AI, Dynamic NS, Pod Metrics
# ─────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{{ cluster_name }} · KubeWatch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Outfit:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
:root {
  --void:       #050709;
  --base:       #080c12;
  --panel:      #0c1018;
  --panel2:     #101520;
  --rim:        #161e2e;
  --rim2:       #1c2538;
  --green:      #22d3a5;
  --green-dim:  rgba(34,211,165,0.12);
  --green-glow: rgba(34,211,165,0.25);
  --amber:      #f59e0b;
  --amber-dim:  rgba(245,158,11,0.12);
  --red:        #f43f5e;
  --red-dim:    rgba(244,63,94,0.12);
  --red-glow:   rgba(244,63,94,0.3);
  --blue:       #38bdf8;
  --blue-dim:   rgba(56,189,248,0.1);
  --violet:     #a78bfa;
  --text:       #e2e8f0;
  --dim:        #64748b;
  --dim2:       #334155;
  --mono:       'Space Mono', monospace;
  --sans:       'Outfit', sans-serif;
  --r:          8px;
  --r2:         12px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--base);
  color: var(--text);
  font-family: var(--sans);
  font-size: 14px;
  min-height: 100vh;
  overflow-x: hidden;
}

body::before {
  content: '';
  position: fixed; inset: 0;
  background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px);
  pointer-events: none; z-index: 1000;
}

::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: var(--base); }
::-webkit-scrollbar-thumb { background: var(--rim2); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--dim); }

/* ═══ TOPBAR ═══ */
.topbar {
  position: sticky; top: 0; z-index: 200;
  background: rgba(8,12,18,0.92);
  backdrop-filter: blur(20px);
  border-bottom: 1px solid var(--rim);
  height: 56px;
  display: flex; align-items: center;
  padding: 0 24px; gap: 16px;
}
.logo { display: flex; align-items: center; gap: 10px; font-family: var(--sans); font-weight: 800; font-size: 17px; letter-spacing: -0.3px; white-space: nowrap; }
.logo-icon { width: 28px; height: 28px; background: linear-gradient(135deg, var(--green), var(--blue)); border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 14px; }
.logo-name { color: var(--text); }
.logo-name span { color: var(--green); }
.cluster-tag { font-family: var(--mono); font-size: 11px; color: var(--blue); background: var(--blue-dim); border: 1px solid rgba(56,189,248,0.2); padding: 3px 10px; border-radius: 20px; }
.topbar-mid { flex: 1; display: flex; align-items: center; gap: 10px; }
.overall-indicator { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: 600; letter-spacing: 0.3px; padding: 5px 14px; border-radius: 20px; border: 1px solid; transition: all 0.3s; }
.overall-indicator.healthy { background: var(--green-dim); border-color: rgba(34,211,165,0.3); color: var(--green); }
.overall-indicator.warning { background: var(--amber-dim); border-color: rgba(245,158,11,0.3); color: var(--amber); }
.overall-indicator.critical { background: var(--red-dim); border-color: rgba(244,63,94,0.4); color: var(--red); animation: blink 2s infinite; }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.5} }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
.status-dot.pulse { animation: dotpulse 2s infinite; }
@keyframes dotpulse { 0%,100%{box-shadow:0 0 0 0 currentColor} 50%{box-shadow:0 0 0 4px transparent} }
.topbar-right { display: flex; align-items: center; gap: 10px; }
.ts-badge { font-family: var(--mono); font-size: 10px; color: var(--dim); }
.icon-btn { width: 32px; height: 32px; border: 1px solid var(--rim2); border-radius: 6px; background: var(--panel2); color: var(--dim); display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 14px; transition: all 0.2s; }
.icon-btn:hover { border-color: var(--blue); color: var(--blue); background: var(--blue-dim); }
.btn { display: flex; align-items: center; gap: 6px; padding: 6px 14px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; border: 1px solid; transition: all 0.2s; font-family: var(--sans); white-space: nowrap; }
.btn-issues { background: var(--red-dim); border-color: rgba(244,63,94,0.3); color: var(--red); }
.btn-issues:hover { background: var(--red-glow); border-color: var(--red); }
.btn-issues.active-mode { background: var(--red); color: white; border-color: var(--red); }

/* ═══ FILTERBAR ═══ */
.filterbar {
  background: var(--panel);
  border-bottom: 1px solid var(--rim);
  padding: 0 24px;
  height: 48px;
  display: flex; align-items: center; gap: 14px;
}
.filter-label { font-size: 11px; color: var(--dim); font-weight: 600; text-transform: uppercase; letter-spacing: 1px; white-space: nowrap; }
.filter-sep { width: 1px; height: 20px; background: var(--rim2); flex-shrink: 0; }

/* Custom dropdown */
.ns-dropdown { position: relative; }
.ns-dropdown-btn {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--rim2); background: var(--panel2);
  color: var(--text); cursor: pointer; font-family: var(--mono);
  font-size: 11px; transition: all 0.15s; min-width: 200px;
  justify-content: space-between;
}
.ns-dropdown-btn:hover { border-color: var(--blue); }
.ns-dropdown-btn.open { border-color: var(--blue); background: var(--blue-dim); }
.ns-dropdown-arrow { font-size: 8px; color: var(--dim); transition: transform 0.2s; }
.ns-dropdown-btn.open .ns-dropdown-arrow { transform: rotate(180deg); }
.ns-dropdown-menu {
  position: absolute; top: calc(100% + 4px); left: 0;
  min-width: 260px; max-height: 400px; overflow-y: auto;
  background: var(--panel); border: 1px solid var(--rim2);
  border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  z-index: 300; display: none;
  padding: 6px 0;
}
.ns-dropdown-menu.show { display: block; }
.ns-dropdown-menu .ns-search {
  padding: 6px 10px; margin: 0 6px 6px; border-bottom: 1px solid var(--rim);
}
.ns-dropdown-menu .ns-search input {
  width: 100%; padding: 5px 8px; background: var(--panel2);
  border: 1px solid var(--rim2); border-radius: 4px;
  color: var(--text); font-family: var(--mono); font-size: 11px; outline: none;
}
.ns-dropdown-menu .ns-search input:focus { border-color: var(--blue); }
.ns-opt {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 14px; cursor: pointer; font-family: var(--mono);
  font-size: 11px; color: var(--dim); transition: all 0.1s;
}
.ns-opt:hover { background: var(--panel2); color: var(--text); }
.ns-opt.selected { color: var(--blue); }
.ns-opt .ns-check { width: 14px; height: 14px; border: 1px solid var(--rim2); border-radius: 3px; display: flex; align-items: center; justify-content: center; font-size: 9px; flex-shrink: 0; }
.ns-opt.selected .ns-check { background: var(--blue); border-color: var(--blue); color: white; }
.ns-opt-all { border-bottom: 1px solid var(--rim); margin-bottom: 4px; padding-bottom: 10px; font-weight: 700; color: var(--text); }
.ns-selected-count { font-size: 10px; color: var(--dim); background: var(--panel2); padding: 1px 6px; border-radius: 8px; margin-left: auto; }

.search-wrap { position: relative; flex: 1; max-width: 280px; }
.search-wrap input { width: 100%; padding: 5px 10px 5px 30px; background: var(--panel2); border: 1px solid var(--rim2); border-radius: 6px; color: var(--text); font-family: var(--mono); font-size: 11px; outline: none; transition: border-color 0.2s; }
.search-wrap input::placeholder { color: var(--dim); }
.search-wrap input:focus { border-color: var(--blue); }
.search-icon { position: absolute; left: 9px; top: 50%; transform: translateY(-50%); color: var(--dim); font-size: 12px; }

/* ═══ LAYOUT ═══ */
.layout { display: flex; height: calc(100vh - 104px); }

.sidenav {
  width: 52px; flex-shrink: 0;
  background: var(--panel);
  border-right: 1px solid var(--rim);
  display: flex; flex-direction: column; align-items: center;
  padding: 16px 0; gap: 4px;
  overflow: visible;
}
.snav-item {
  position: relative;
  width: 36px; height: 36px;
  border: 1px solid transparent; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
  cursor: pointer; font-size: 16px; transition: all 0.15s;
  color: var(--dim);
}
.snav-item:hover { border-color: var(--rim2); color: var(--text); background: var(--panel2); }
.snav-item.active { border-color: var(--blue); color: var(--blue); background: var(--blue-dim); }
.snav-item .badge {
  position: absolute; top: -4px; right: -4px;
  background: var(--red); color: white;
  border-radius: 8px; font-size: 9px; font-family: var(--mono);
  padding: 1px 4px; min-width: 16px; text-align: center;
  border: 1px solid var(--base);
}
.snav-tooltip {
  position: absolute; left: calc(100% + 10px); top: 50%; transform: translateY(-50%);
  background: var(--panel2); border: 1px solid var(--rim2);
  border-radius: 6px; padding: 5px 10px;
  font-size: 11px; white-space: nowrap; color: var(--text);
  pointer-events: none; opacity: 0; transition: opacity 0.15s;
  z-index: 300;
}
.snav-item:hover .snav-tooltip { opacity: 1; }

.content { flex: 1; overflow-y: auto; padding: 20px 24px; }

/* ═══ SUMMARY ═══ */
.summary-strip { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 20px; }
@media(max-width:1100px){ .summary-strip { grid-template-columns: repeat(3,1fr); } }
.sum-card {
  background: var(--panel); border: 1px solid var(--rim); border-radius: var(--r2);
  padding: 14px 16px; cursor: pointer; transition: all 0.2s; position: relative; overflow: hidden;
}
.sum-card::after { content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 2px; background: var(--green); transition: background 0.3s; }
.sum-card.has-crit::after { background: var(--red); }
.sum-card.has-warn::after { background: var(--amber); }
.sum-card:hover { border-color: var(--rim2); transform: translateY(-1px); }
.sum-label { font-size: 10px; text-transform: uppercase; letter-spacing: 1.2px; color: var(--dim); margin-bottom: 6px; font-weight: 600; }
.sum-num { font-size: 26px; font-weight: 800; line-height: 1; margin-bottom: 8px; }
.sum-row { display: flex; gap: 8px; }
.stat { font-family: var(--mono); font-size: 10px; }
.stat.g { color: var(--green); }
.stat.y { color: var(--amber); }
.stat.r { color: var(--red); }

/* ═══ VIEWS ═══ */
.view { display: none; }
.view.active { display: block; }

/* ═══ NODE GRID ═══ */
.nodes-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 14px; }
.node-card { background: var(--panel); border: 1px solid var(--rim); border-radius: var(--r2); padding: 18px; transition: all 0.2s; }
.node-card:hover { border-color: var(--rim2); }
.node-card.warning { border-color: rgba(245,158,11,0.3); }
.node-card.critical { border-color: rgba(244,63,94,0.35); box-shadow: 0 0 20px rgba(244,63,94,0.08); }
.nc-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 14px; }
.nc-name { font-family: var(--mono); font-size: 13px; font-weight: 700; margin-bottom: 4px; }
.nc-role { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--dim); }
.nc-ver { font-family: var(--mono); font-size: 10px; color: var(--dim); margin-bottom: 12px; }
.metric-block { margin-bottom: 10px; }
.metric-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }
.metric-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--dim); }
.metric-val { font-family: var(--mono); font-size: 11px; }
.metric-val.ok   { color: var(--green); }
.metric-val.warn { color: var(--amber); }
.metric-val.crit { color: var(--red); }
.prog-track { height: 5px; background: var(--rim2); border-radius: 3px; overflow: hidden; }
.prog-fill { height: 100%; border-radius: 3px; transition: width 0.6s ease; }
.prog-fill.ok   { background: linear-gradient(90deg, var(--green), rgba(34,211,165,0.6)); }
.prog-fill.warn { background: linear-gradient(90deg, var(--amber), rgba(245,158,11,0.6)); }
.prog-fill.crit { background: linear-gradient(90deg, var(--red), rgba(244,63,94,0.6)); }
.issue-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 10px; }
.chip { font-family: var(--mono); font-size: 9px; padding: 2px 7px; border-radius: 3px; border: 1px solid; letter-spacing: 0.3px; }
.chip.r { background: var(--red-dim); border-color: rgba(244,63,94,0.3); color: var(--red); }
.chip.y { background: var(--amber-dim); border-color: rgba(245,158,11,0.3); color: var(--amber); }
.chip.b { background: var(--blue-dim); border-color: rgba(56,189,248,0.2); color: var(--blue); }

/* ═══ TABLES ═══ */
.ns-block { margin-bottom: 24px; }
.ns-hd { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.ns-hd-tag { font-family: var(--mono); font-size: 11px; background: var(--blue-dim); border: 1px solid rgba(56,189,248,0.2); color: var(--blue); padding: 3px 10px; border-radius: 4px; }
.ns-hd-count { font-size: 11px; color: var(--dim); }
.ns-hd-crit { color: var(--red); margin-left: 4px; }
.tbl-wrap { background: var(--panel); border: 1px solid var(--rim); border-radius: var(--r2); overflow-x: auto; }
.kt { width: 100%; border-collapse: collapse; font-size: 12px; }
.kt thead tr { border-bottom: 1px solid var(--rim); }
.kt th { padding: 9px 14px; text-align: left; font-family: var(--mono); font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--dim); font-weight: 400; background: var(--panel2); white-space: nowrap; }
.kt td { padding: 10px 14px; border-bottom: 1px solid rgba(22,30,46,0.7); vertical-align: middle; }
.kt tr:last-child td { border-bottom: none; }
.kt tbody tr:hover td { background: rgba(255,255,255,0.015); }
.kt tbody tr.clickable { cursor: pointer; }
.mn { font-family: var(--mono); font-size: 12px; }
.dim-text { font-family: var(--mono); font-size: 11px; color: var(--dim); }

/* STATUS PILL */
.pill { display: inline-flex; align-items: center; gap: 5px; padding: 3px 9px; border-radius: 10px; font-family: var(--mono); font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; white-space: nowrap; }
.pill::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.pill.healthy  { background: var(--green-dim); color: var(--green); }
.pill.warning  { background: var(--amber-dim); color: var(--amber); }
.pill.critical { background: var(--red-dim);   color: var(--red); }

.rep { font-family: var(--mono); font-size: 12px; }
.rep.ok   { color: var(--green); }
.rep.warn { color: var(--amber); }
.rep.bad  { color: var(--red); }

/* ═══ POD RESOURCE BARS ═══ */
.res-bar-wrap { display: flex; align-items: center; gap: 6px; min-width: 130px; }
.res-bar-track { flex: 1; height: 6px; background: var(--rim2); border-radius: 3px; overflow: hidden; min-width: 60px; }
.res-bar-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
.res-bar-fill.ok   { background: var(--green); }
.res-bar-fill.warn { background: var(--amber); }
.res-bar-fill.crit { background: var(--red); }
.res-bar-text { font-family: var(--mono); font-size: 10px; white-space: nowrap; min-width: 42px; text-align: right; }
.res-detail { font-family: var(--mono); font-size: 9px; color: var(--dim); margin-top: 2px; }

/* ═══ LOGS ═══ */
.log-row td { padding: 0 !important; border-bottom: 2px solid rgba(244,63,94,0.2) !important; }
.log-pre { background: #040608; color: #94a3b8; font-family: var(--mono); font-size: 10.5px; padding: 12px 18px; margin: 0; border-left: 3px solid var(--red); overflow-x: auto; white-space: pre-wrap; word-break: break-all; max-height: 280px; overflow-y: auto; line-height: 1.6; }
.log-indicator { font-size: 10px; color: var(--dim); margin-left: 6px; }

/* ═══ ISSUES VIEW ═══ */
.issues-view-header { display: flex; align-items: center; gap: 12px; padding: 12px 18px; margin-bottom: 16px; background: var(--red-dim); border: 1px solid rgba(244,63,94,0.25); border-radius: var(--r2); }
.issues-view-title { font-weight: 700; font-size: 14px; color: var(--red); }
.issues-view-count { font-family: var(--mono); font-size: 12px; color: var(--dim); }

/* ═══ MISC ═══ */
.empty-state { text-align: center; padding: 48px 24px; color: var(--dim); }
.empty-icon { font-size: 36px; margin-bottom: 10px; }
.empty-label { font-size: 13px; }
.loading-state { display: flex; align-items: center; justify-content: center; height: 160px; gap: 14px; color: var(--dim); font-size: 13px; }
.spin { width: 24px; height: 24px; border: 2px solid var(--rim2); border-top-color: var(--blue); border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to{transform:rotate(360deg)} }
.section-hd { display: flex; align-items: center; gap: 10px; font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: var(--dim); font-weight: 700; margin: 20px 0 12px; }
.section-hd::after { content:''; flex:1; height:1px; background: var(--rim); }
.err-bar { background: var(--red-dim); border: 1px solid rgba(244,63,94,0.3); border-radius: 6px; padding: 10px 16px; margin-bottom: 16px; color: var(--red); font-size: 12px; display: none; }
[data-tip] { position: relative; }
[data-tip]::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 5px); left: 50%; transform: translateX(-50%); background: var(--panel2); border: 1px solid var(--rim2); border-radius: 4px; font-size: 10px; padding: 3px 7px; white-space: nowrap; color: var(--text); pointer-events: none; opacity: 0; transition: opacity 0.15s; z-index: 400; }
[data-tip]:hover::after { opacity: 1; }
</style>
</head>
<body>

<!-- ═══ TOP BAR ═══ -->
<div class="topbar">
  <div class="logo">
    <div class="logo-icon">⎈</div>
    <div class="logo-name">Kube<span>Watch</span></div>
  </div>
  <div class="cluster-tag" id="clusterTag">{{ cluster_name }}</div>
  <div class="topbar-mid">
    <div class="overall-indicator healthy" id="overallBadge">
      <div class="status-dot"></div>
      <span id="overallText">Loading…</span>
    </div>
  </div>
  <div class="topbar-right">
    <span class="ts-badge" id="tsBadge">—</span>
    <button class="btn btn-issues" id="issuesToggle" onclick="toggleIssuesMode()">⚠ Issues Only</button>
    <button class="icon-btn" onclick="loadData()" data-tip="Refresh">↻</button>
  </div>
</div>

<!-- ═══ FILTER BAR ═══ -->
<div class="filterbar">
  <span class="filter-label">Namespaces</span>

  <!-- Namespace Dropdown -->
  <div class="ns-dropdown" id="nsDropdown">
    <div class="ns-dropdown-btn" id="nsDropdownBtn" onclick="toggleNsDropdown()">
      <span id="nsDropdownLabel">All Namespaces</span>
      <span class="ns-dropdown-arrow">▼</span>
    </div>
    <div class="ns-dropdown-menu" id="nsDropdownMenu">
      <div class="ns-search">
        <input type="text" id="nsSearchInput" placeholder="Search namespaces…" oninput="filterNsOptions()">
      </div>
      <div id="nsOptionsList"></div>
    </div>
  </div>

  <div class="filter-sep"></div>

  <div class="search-wrap">
    <span class="search-icon">⌕</span>
    <input type="text" id="searchInput" placeholder="Filter by name…" oninput="applySearch()">
  </div>
</div>

<!-- ═══ BODY ═══ -->
<div id="contentArea">
  <div class="layout">
    <nav class="sidenav">
      <div class="snav-item active" onclick="switchView('nodes')" id="snav-nodes">
        🖥<div class="snav-tooltip">Nodes</div>
        <span class="badge" id="snav-badge-nodes" style="display:none"></span>
      </div>
      <div class="snav-item" onclick="switchView('pods')" id="snav-pods">
        📦<div class="snav-tooltip">Pods</div>
        <span class="badge" id="snav-badge-pods" style="display:none"></span>
      </div>
      <div class="snav-item" onclick="switchView('deployments')" id="snav-deployments">
        🚀<div class="snav-tooltip">Deployments</div>
        <span class="badge" id="snav-badge-deployments" style="display:none"></span>
      </div>
      <div class="snav-item" onclick="switchView('statefulsets')" id="snav-statefulsets">
        🗃<div class="snav-tooltip">StatefulSets</div>
        <span class="badge" id="snav-badge-statefulsets" style="display:none"></span>
      </div>
      <div class="snav-item" onclick="switchView('pvcs')" id="snav-pvcs">
        💾<div class="snav-tooltip">PVCs</div>
        <span class="badge" id="snav-badge-pvcs" style="display:none"></span>
      </div>
      <div class="snav-item" onclick="switchView('jobs')" id="snav-jobs">
        ⚙<div class="snav-tooltip">Jobs / CronJobs</div>
        <span class="badge" id="snav-badge-jobs" style="display:none"></span>
      </div>
    </nav>

    <div class="content" id="mainContent">
      <div class="err-bar" id="errBar"></div>
      <div class="summary-strip" id="summaryStrip">
        <div class="loading-state"><div class="spin"></div> Loading cluster data…</div>
      </div>
      <div class="view active" id="view-nodes"></div>
      <div class="view" id="view-pods"></div>
      <div class="view" id="view-deployments"></div>
      <div class="view" id="view-statefulsets"></div>
      <div class="view" id="view-pvcs"></div>
      <div class="view" id="view-jobs"></div>
    </div>
  </div>
</div>

<script>
// ═══════════════════════════
// STATE
// ═══════════════════════════
let state         = null;
let currentView   = 'nodes';
let issuesMode    = false;
let searchQuery   = '';
let autoRefreshId = null;

// Namespace selection
let allNamespaces    = [];
let selectedNS       = new Set();  // empty = all
let nsDropdownOpen   = false;

// ═══════════════════════════
// UTILS
// ═══════════════════════════
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function pill(s, label) { return `<span class="pill ${s}">${label || s}</span>`; }
function repClass(avail, desired) { return avail >= desired ? 'ok' : avail === 0 ? 'bad' : 'warn'; }
function pctClass(pct) { return pct > 85 ? 'crit' : pct > 70 ? 'warn' : 'ok'; }

function filterItems(items) {
  if (!items) return [];
  let list = items;
  if (issuesMode) list = list.filter(i => i.status !== 'healthy');
  if (searchQuery) list = list.filter(i => i.name && i.name.toLowerCase().includes(searchQuery));
  return list;
}
function filterNsData(nsData) {
  const out = {};
  for (const [ns, items] of Object.entries(nsData)) {
    out[ns] = filterItems(items);
  }
  return out;
}

// ═══════════════════════════
// NAMESPACE DROPDOWN
// ═══════════════════════════
function toggleNsDropdown() {
  nsDropdownOpen = !nsDropdownOpen;
  document.getElementById('nsDropdownBtn').classList.toggle('open', nsDropdownOpen);
  document.getElementById('nsDropdownMenu').classList.toggle('show', nsDropdownOpen);
  if (nsDropdownOpen) {
    document.getElementById('nsSearchInput').focus();
  }
}

// Close dropdown on outside click
document.addEventListener('click', function(e) {
  const dd = document.getElementById('nsDropdown');
  if (dd && !dd.contains(e.target)) {
    nsDropdownOpen = false;
    document.getElementById('nsDropdownBtn').classList.remove('open');
    document.getElementById('nsDropdownMenu').classList.remove('show');
  }
});

function renderNsOptions(filter) {
  filter = (filter || '').toLowerCase();
  const list = document.getElementById('nsOptionsList');
  const filtered = allNamespaces.filter(ns => ns.toLowerCase().includes(filter));
  const isAll = selectedNS.size === 0;

  let html = `<div class="ns-opt ns-opt-all ${isAll ? 'selected' : ''}" onclick="selectAllNS()">
    <span class="ns-check">${isAll ? '✓' : ''}</span>
    All Namespaces
    <span class="ns-selected-count">${allNamespaces.length}</span>
  </div>`;

  for (const ns of filtered) {
    const sel = selectedNS.has(ns);
    html += `<div class="ns-opt ${sel ? 'selected' : ''}" onclick="toggleNS('${esc(ns)}')">
      <span class="ns-check">${sel ? '✓' : ''}</span>
      ${esc(ns)}
    </div>`;
  }
  list.innerHTML = html;
}

function filterNsOptions() {
  const v = document.getElementById('nsSearchInput').value;
  renderNsOptions(v);
}

function selectAllNS() {
  selectedNS.clear();
  updateNsLabel();
  renderNsOptions(document.getElementById('nsSearchInput').value);
  loadData();
}

function toggleNS(ns) {
  if (selectedNS.has(ns)) {
    selectedNS.delete(ns);
  } else {
    selectedNS.add(ns);
  }
  // If all are selected, treat as "all"
  if (selectedNS.size === allNamespaces.length) {
    selectedNS.clear();
  }
  updateNsLabel();
  renderNsOptions(document.getElementById('nsSearchInput').value);
  loadData();
}

function updateNsLabel() {
  const label = document.getElementById('nsDropdownLabel');
  if (selectedNS.size === 0) {
    label.textContent = 'All Namespaces';
  } else if (selectedNS.size === 1) {
    label.textContent = [...selectedNS][0];
  } else {
    label.textContent = `${selectedNS.size} namespaces selected`;
  }
}

async function loadNamespaces() {
  try {
    const res = await fetch('/api/namespaces');
    const data = await res.json();
    allNamespaces = data.namespaces || [];
    renderNsOptions();
  } catch(e) {
    console.error('Failed to load namespaces', e);
  }
}

// ═══════════════════════════
// NAV & FILTERS
// ═══════════════════════════
function switchView(name) {
  currentView = name;
  document.querySelectorAll('.snav-item').forEach(el => el.classList.remove('active'));
  document.getElementById('snav-' + name).classList.add('active');
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  if (state) renderCurrentView();
}

function applySearch() {
  searchQuery = document.getElementById('searchInput').value.toLowerCase().trim();
  if (state) renderCurrentView();
}

function toggleIssuesMode() {
  issuesMode = !issuesMode;
  const btn = document.getElementById('issuesToggle');
  btn.classList.toggle('active-mode', issuesMode);
  btn.textContent = issuesMode ? '✕ Show All' : '⚠ Issues Only';
  if (state) renderAll();
}

// ═══════════════════════════
// RENDER SUMMARY
// ═══════════════════════════
function renderSummary(summary) {
  const items = [
    {key:'nodes',label:'Nodes',icon:'🖥'},
    {key:'pods',label:'Pods',icon:'📦'},
    {key:'deployments',label:'Deployments',icon:'🚀'},
    {key:'statefulsets',label:'StatefulSets',icon:'🗃'},
    {key:'pvcs',label:'PVCs',icon:'💾'},
    {key:'jobs',label:'Jobs',icon:'⚙'},
  ];
  document.getElementById('summaryStrip').innerHTML = items.map(item => {
    const s = summary[item.key] || {};
    const cls = s.critical > 0 ? 'has-crit' : s.warning > 0 ? 'has-warn' : '';
    return `<div class="sum-card ${cls}" onclick="switchView('${item.key}')">
      <div class="sum-label">${item.icon} ${item.label}</div>
      <div class="sum-num">${s.total || 0}</div>
      <div class="sum-row">
        <span class="stat g">✓${s.healthy||0}</span>
        ${s.warning  > 0 ? `<span class="stat y">⚠${s.warning}</span>` : ''}
        ${s.critical > 0 ? `<span class="stat r">✕${s.critical}</span>` : ''}
      </div>
    </div>`;
  }).join('');
}

// ═══════════════════════════
// RENDER NODES
// ═══════════════════════════
function renderNodes() {
  const el = document.getElementById('view-nodes');
  let nodes = state.nodes || [];
  if (searchQuery) nodes = nodes.filter(n => n.name.toLowerCase().includes(searchQuery));
  if (issuesMode) nodes = nodes.filter(n => n.status !== 'healthy');

  if (!nodes.length) {
    el.innerHTML = '<div class="empty-state"><div class="empty-icon">🖥️</div><div class="empty-label">No nodes found</div></div>';
    return;
  }

  el.innerHTML = `<div class="nodes-grid">${nodes.map(n => {
    const cpuPct = n.cpu_pct || 0;
    const memPct = n.mem_pct || 0;
    const cpuCls = pctClass(cpuPct);
    const memCls = pctClass(memPct);

    return `<div class="node-card ${n.status}">
      <div class="nc-head">
        <div>
          <div class="nc-name">${esc(n.name)}</div>
          <div class="nc-role">${n.roles.join(' · ')}</div>
        </div>
        ${pill(n.status)}
      </div>
      <div class="nc-ver">${esc(n.version || '')} · ${esc(n.arch || '')} · ${esc(n.os || '')}</div>

      ${n.cpu_pct !== null ? `
      <div class="metric-block">
        <div class="metric-head">
          <span class="metric-label">CPU</span>
          <span class="metric-val ${cpuCls}">${cpuPct}% · ${n.cpu_used_m}m / ${n.alloc_cpu_m}m</span>
        </div>
        <div class="prog-track"><div class="prog-fill ${cpuCls}" style="width:${Math.min(cpuPct,100)}%"></div></div>
      </div>
      <div class="metric-block">
        <div class="metric-head">
          <span class="metric-label">Memory</span>
          <span class="metric-val ${memCls}">${memPct}% · ${n.mem_used_mi}Mi / ${n.alloc_mem_mi}Mi</span>
        </div>
        <div class="prog-track"><div class="prog-fill ${memCls}" style="width:${Math.min(memPct,100)}%"></div></div>
      </div>
      ` : '<div style="font-size:11px;color:var(--dim);margin-bottom:10px">Metrics server unavailable</div>'}

      ${n.issues.length ? `<div class="issue-chips">${n.issues.map(i => `<span class="chip r">${esc(i)}</span>`).join('')}</div>` : ''}
    </div>`;
  }).join('')}</div>`;
}

// ═══════════════════════════
// RENDER PODS (with CPU/RAM bars)
// ═══════════════════════════
function renderResBar(usedLabel, pct, cls) {
  if (pct === null || pct === undefined) {
    return `<span class="dim-text">N/A</span>`;
  }
  const barCls = pctClass(pct);
  return `<div>
    <div class="res-bar-wrap">
      <div class="res-bar-track"><div class="res-bar-fill ${barCls}" style="width:${Math.min(pct,100)}%"></div></div>
      <span class="res-bar-text" style="color:var(--${barCls === 'ok' ? 'green' : barCls === 'warn' ? 'amber' : 'red'})">${pct}%</span>
    </div>
    <div class="res-detail">${usedLabel}</div>
  </div>`;
}

function renderPods() {
  const el = document.getElementById('view-pods');
  const nsData = filterNsData(state.pods);

  if (issuesMode) {
    const allIssues = Object.values(nsData).flat().filter(p => p.status !== 'healthy');
    el.innerHTML = `<div class="issues-view-header">
      <span>⚠</span><span class="issues-view-title">Issues Only</span>
      <span class="issues-view-count">${allIssues.length} affected pod(s)</span>
    </div>` + renderNsSection({'all-namespaces': allIssues}, buildPodTable);
    return;
  }
  el.innerHTML = renderNsSection(nsData, buildPodTable);
}

function buildPodTable(pods) {
  if (!pods.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">All pods healthy</div></div>';
  return `<div class="tbl-wrap"><table class="kt">
    <thead><tr>
      <th>Name</th><th>Status</th><th>Phase</th>
      <th>CPU Usage</th><th>Memory Usage</th>
      <th>Restarts</th><th>Node</th><th>Age</th><th>Issues</th>
    </tr></thead>
    <tbody>
    ${pods.map(p => {
      const hasLogs = p.logs && p.logs.trim();
      const rowId = 'log-' + (p.namespace||'') + '-' + p.name.replace(/[^a-z0-9]/gi,'-');

      // CPU bar
      const cpuUsed = p.cpu_used_m;
      const cpuReq  = p.req_cpu_m;
      const cpuLim  = p.lim_cpu_m;
      let cpuLabel = '';
      let cpuPct = null;
      if (cpuUsed !== null && cpuUsed !== undefined) {
        cpuLabel = cpuUsed + 'm';
        if (cpuLim > 0) { cpuLabel += ' / ' + cpuLim + 'm'; cpuPct = p.cpu_pct_lim; }
        else if (cpuReq > 0) { cpuLabel += ' / ' + cpuReq + 'm req'; cpuPct = p.cpu_pct_req; }
      }

      // Mem bar
      const memUsed = p.mem_used_mi;
      const memReq  = p.req_mem_mi;
      const memLim  = p.lim_mem_mi;
      let memLabel = '';
      let memPct = null;
      if (memUsed !== null && memUsed !== undefined) {
        memLabel = memUsed + 'Mi';
        if (memLim > 0) { memLabel += ' / ' + memLim + 'Mi'; memPct = p.mem_pct_lim; }
        else if (memReq > 0) { memLabel += ' / ' + memReq + 'Mi req'; memPct = p.mem_pct_req; }
      }

      return `
      <tr class="${hasLogs ? 'clickable' : ''}" onclick="${hasLogs ? `toggleLog('${rowId}')` : ''}">
        <td class="mn">${esc(p.name)}${hasLogs ? '<span class="log-indicator">▶ logs</span>' : ''}</td>
        <td>${pill(p.status)}</td>
        <td class="dim-text">${esc(p.phase)}</td>
        <td>${renderResBar(cpuLabel, cpuPct)}</td>
        <td>${renderResBar(memLabel, memPct)}</td>
        <td class="dim-text" style="color:${p.restarts > 3 ? 'var(--red)' : 'var(--dim)'}">${p.restarts}</td>
        <td class="dim-text">${esc(p.node)}</td>
        <td class="dim-text">${esc(p.age)}</td>
        <td>${p.issues.map(i => `<span class="chip r">${esc(i)}</span>`).join('') || '<span class="dim-text">—</span>'}</td>
      </tr>
      ${hasLogs ? `<tr class="log-row" id="${rowId}" style="display:none">
        <td colspan="9"><pre class="log-pre">${esc(p.logs)}</pre></td>
      </tr>` : ''}`;
    }).join('')}
    </tbody></table></div>`;
}

// ═══════════════════════════
// RENDER DEPLOYMENTS
// ═══════════════════════════
function renderDeployments() {
  const el = document.getElementById('view-deployments');
  const nsData = filterNsData(state.deployments);
  el.innerHTML = renderNsSection(nsData, deps => {
    if (!deps.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">All deployments healthy</div></div>';
    return `<div class="tbl-wrap"><table class="kt">
      <thead><tr><th>Name</th><th>Status</th><th>Desired</th><th>Available</th><th>Ready</th><th>Image</th><th>Age</th></tr></thead>
      <tbody>${deps.map(d => {
        const rc = repClass(d.available, d.desired);
        const imgShort = d.image ? d.image.split('/').pop() : '—';
        return `<tr>
          <td class="mn">${esc(d.name)}</td>
          <td>${pill(d.status)}</td>
          <td class="dim-text">${d.desired}</td>
          <td><span class="rep ${rc}">${d.available}</span></td>
          <td class="dim-text">${d.ready}</td>
          <td class="dim-text" style="max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${esc(d.image)}">${esc(imgShort)}</td>
          <td class="dim-text">${esc(d.age)}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  });
}

// ═══════════════════════════
// RENDER STATEFULSETS
// ═══════════════════════════
function renderStatefulSets() {
  const el = document.getElementById('view-statefulsets');
  const nsData = filterNsData(state.statefulsets);
  el.innerHTML = renderNsSection(nsData, sets => {
    if (!sets.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">No StatefulSets</div></div>';
    return `<div class="tbl-wrap"><table class="kt">
      <thead><tr><th>Name</th><th>Status</th><th>Desired</th><th>Ready</th><th>Age</th></tr></thead>
      <tbody>${sets.map(s => {
        const rc = repClass(s.ready, s.desired);
        return `<tr>
          <td class="mn">${esc(s.name)}</td>
          <td>${pill(s.status)}</td>
          <td class="dim-text">${s.desired}</td>
          <td><span class="rep ${rc}">${s.ready}</span></td>
          <td class="dim-text">${esc(s.age)}</td>
        </tr>`;
      }).join('')}</tbody></table></div>`;
  });
}

// ═══════════════════════════
// RENDER PVCs
// ═══════════════════════════
function renderPVCs() {
  const el = document.getElementById('view-pvcs');
  const nsData = filterNsData(state.pvcs);
  el.innerHTML = renderNsSection(nsData, pvcs => {
    if (!pvcs.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">No PVCs</div></div>';
    return `<div class="tbl-wrap"><table class="kt">
      <thead><tr><th>Name</th><th>Status</th><th>Phase</th><th>Storage Class</th><th>Size</th><th>Age</th></tr></thead>
      <tbody>${pvcs.map(p => `<tr>
        <td class="mn">${esc(p.name)}</td>
        <td>${pill(p.status)}</td>
        <td class="dim-text">${esc(p.phase)}</td>
        <td class="dim-text">${esc(p.storage_class)}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--blue)">${esc(p.storage)}</td>
        <td class="dim-text">${esc(p.age)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  });
}

// ═══════════════════════════
// RENDER JOBS
// ═══════════════════════════
function renderJobs() {
  const el = document.getElementById('view-jobs');
  let html = '';

  const cjData = filterNsData(state.cronjobs);
  html += '<div class="section-hd">CronJobs</div>';
  html += renderNsSection(cjData, cjs => {
    if (!cjs.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">No CronJobs</div></div>';
    return `<div class="tbl-wrap"><table class="kt">
      <thead><tr><th>Name</th><th>Status</th><th>Schedule</th><th>Last Run</th><th>Active</th></tr></thead>
      <tbody>${cjs.map(cj => `<tr>
        <td class="mn">${esc(cj.name)}</td>
        <td>${pill(cj.status, cj.suspended ? 'Suspended' : 'Active')}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--blue)">${esc(cj.schedule)}</td>
        <td class="dim-text">${esc(cj.last_run)}</td>
        <td class="dim-text">${cj.active}</td>
      </tr>`).join('')}</tbody></table></div>`;
  });

  const jobData = filterNsData(state.jobs);
  html += '<div class="section-hd" style="margin-top:24px">Jobs</div>';
  html += renderNsSection(jobData, jobs => {
    if (!jobs.length) return '<div class="empty-state" style="padding:24px"><div class="empty-icon">✓</div><div class="empty-label">No Jobs</div></div>';
    return `<div class="tbl-wrap"><table class="kt">
      <thead><tr><th>Name</th><th>Status</th><th>Active</th><th>Succeeded</th><th>Failed</th><th>Age</th></tr></thead>
      <tbody>${jobs.map(j => `<tr>
        <td class="mn">${esc(j.name)}</td>
        <td>${pill(j.status)}</td>
        <td class="dim-text">${j.active}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--green)">${j.succeeded}</td>
        <td style="font-family:var(--mono);font-size:11px;color:${j.failed > 0 ? 'var(--red)' : 'var(--dim)'}">${j.failed}</td>
        <td class="dim-text">${esc(j.age)}</td>
      </tr>`).join('')}</tbody></table></div>`;
  });

  el.innerHTML = html;
}

// ═══════════════════════════
// SHARED NS RENDERER
// ═══════════════════════════
function renderNsSection(nsData, tableBuilder) {
  const namespaces = Object.keys(nsData);
  if (!namespaces.length) return '<div class="empty-state"><div class="empty-icon">📭</div><div class="empty-label">No data for selected namespaces</div></div>';
  return namespaces.map(ns => {
    const items = nsData[ns] || [];
    const critCount = items.filter(i => i.status === 'critical').length;
    return `<div class="ns-block">
      <div class="ns-hd">
        <span class="ns-hd-tag">${esc(ns)}</span>
        <span class="ns-hd-count">${items.length} item${items.length !== 1 ? 's' : ''}
          ${critCount > 0 ? `<span class="ns-hd-crit">· ${critCount} critical</span>` : ''}
        </span>
      </div>
      ${tableBuilder(items)}
    </div>`;
  }).join('');
}

function toggleLog(id) {
  const row = document.getElementById(id);
  if (row) row.style.display = row.style.display === 'none' ? 'table-row' : 'none';
}

// ═══════════════════════════
// RENDER ALL
// ═══════════════════════════
function renderCurrentView() {
  const renderers = {
    nodes: renderNodes, pods: renderPods, deployments: renderDeployments,
    statefulsets: renderStatefulSets, pvcs: renderPVCs, jobs: renderJobs,
  };
  renderers[currentView]?.();
}

function renderAll() {
  renderNodes(); renderPods(); renderDeployments();
  renderStatefulSets(); renderPVCs(); renderJobs();
}

// ═══════════════════════════
// BADGES
// ═══════════════════════════
function updateBadges(summary) {
  const map = { nodes: summary.nodes, pods: summary.pods, deployments: summary.deployments,
    statefulsets: summary.statefulsets, pvcs: summary.pvcs, jobs: summary.jobs };
  for (const [key, s] of Object.entries(map)) {
    const el = document.getElementById('snav-badge-' + key);
    const n = s?.critical || 0;
    if (el) { el.textContent = n; el.style.display = n > 0 ? 'block' : 'none'; }
  }
}

// ═══════════════════════════
// DATA FETCH
// ═══════════════════════════
async function loadData() {
  try {
    document.getElementById('errBar').style.display = 'none';

    // Build query string with selected namespaces
    let url = '/api/status';
    if (selectedNS.size > 0) {
      url += '?namespaces=' + encodeURIComponent([...selectedNS].join(','));
    }

    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state = await res.json();

    const badge = document.getElementById('overallBadge');
    badge.className = `overall-indicator ${state.overall}`;
    const dot = badge.querySelector('.status-dot');
    dot.className = `status-dot${state.overall === 'critical' ? ' pulse' : ''}`;
    document.getElementById('overallText').textContent =
      state.overall === 'healthy' ? '✓ All Systems Operational' :
      state.overall === 'warning' ? '⚠ Warnings Detected' : '✕ Critical Issues';

    document.getElementById('tsBadge').textContent = new Date(state.timestamp).toLocaleTimeString();

    renderSummary(state.summary);
    updateBadges(state.summary);
    renderAll();

  } catch (err) {
    const bar = document.getElementById('errBar');
    bar.textContent = '⚠ Failed to load: ' + err.message;
    bar.style.display = 'block';
  }
}

// ═══════════════════════════
// BOOT
// ═══════════════════════════
loadNamespaces();
loadData();
autoRefreshId = setInterval(loadData, 30000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.getenv("STATUS_PORT", 8080))
    logger.info("KubeWatch starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)

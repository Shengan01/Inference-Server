#!/usr/bin/env python3
"""Capture evidence (command output and web UI screenshots) for the task 3 report.

* "cmd" steps run a shell command against the live cluster and render the real
  output as a terminal-style PNG (docs/img/<id>.png).
* "web" steps take a real screenshot of a URL with headless Google Chrome
  (Grafana, Prometheus, Alertmanager, GitHub Actions).

Prerequisites: running Minikube cluster with the stack deployed, google-chrome,
kubectl, curl, `pip install websockets`. Credentials (Grafana password) are read from the cluster Secret
at runtime and are never written to the images.

Usage: python3 scripts/capture_evidence.py [step_id ...]   (default: all steps)
"""
import base64
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG = ROOT / "docs" / "img"
NS = "inference-monitoring"
HOST = "inference.local"

PY_TARGETS = ("python3 -c \"import sys,json; [print(t['scrapePool'], t['health']) "
              "for t in json.load(sys.stdin)['data']['activeTargets']]\"")
PY_OP_TARGETS = ("python3 -c \"import sys,json; [print(t['scrapePool'], t['health']) "
                 "for t in json.load(sys.stdin)['data']['activeTargets'] if 'inference-monitoring' in t['scrapePool']]\"")

# (id, title, kind, payload, options)
STEPS = [
    # ---- A. deployment confirmation -------------------------------------------------------
    ("a01_cluster", "Klaster i namespace", "cmd", "kubectl get nodes -o wide && kubectl get namespaces", {}),
    ("a02_workloads", "Deployment / StatefulSet / Pod", "cmd",
     f"kubectl -n {NS} get deployments,statefulsets,pods", {}),
    ("a03_services_ingress", "Service i Ingress", "cmd", f"kubectl -n {NS} get services,ingress", {}),
    ("a04_storage", "PersistentVolumeClaim i PersistentVolume", "cmd",
     f"kubectl -n {NS} get pvc && kubectl get pv", {}),
    ("a05_config_secrets", "ConfigMap i Secret (nazwy i typy, bez wartości)", "cmd",
     f"kubectl -n {NS} get configmaps,secrets", {}),
    ("a06_networkpolicy", "NetworkPolicy", "cmd", f"kubectl -n {NS} get networkpolicies", {}),
    ("a07_resourcequota", "ResourceQuota", "cmd",
     f"kubectl -n {NS} describe resourcequota inference-monitoring-quota", {}),
    ("a08_limitrange", "LimitRange", "cmd", f"kubectl -n {NS} describe limitrange inference-monitoring-limits", {}),
    ("a09_requests_limits", "Requests i limits każdego kontenera", "cmd",
     f"kubectl -n {NS} get pods -o custom-columns=POD:.metadata.name,CONTAINER:.spec.containers[*].name,"
     "CPU_REQ:.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.containers[*].resources.limits.cpu,"
     "MEM_REQ:.spec.containers[*].resources.requests.memory,MEM_LIM:.spec.containers[*].resources.limits.memory", {}),
    ("a10_ingress_access", "Dostęp przez Ingress (TLS, przekierowanie HTTP -> HTTPS)", "cmd",
     "IP=$(minikube ip); for p in grafana/api/health v2/health/ready mcp/health; do "
     "echo \"https://inference.local/$p -> $(curl -sk --resolve inference.local:443:$IP "
     "https://inference.local/$p -o /dev/null -w '%{http_code}')\"; done; "
     "echo \"http://inference.local/grafana/ -> $(curl -s --resolve inference.local:80:$IP "
     "http://inference.local/grafana/ -o /dev/null -w '%{http_code}') (przekierowanie na HTTPS)\"", {}),
    ("a11_gitops", "GitOps: aplikacje ArgoCD i profil operatora", "cmd",
     "kubectl -n argocd get applications && kubectl -n monitoring get pods", {}),
    ("a12_gpu_node", "GPU w klastrze: zasób nvidia.com/gpu i karta widoczna w podzie Tritona", "cmd",
     "kubectl describe node | grep -E 'nvidia.com/gpu' ; echo; "
     f"kubectl -n {NS} exec deploy/triton-server -c triton-server -- nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv", {}),
    ("a13_triton_real", "Prawdziwy Triton + vLLM: model załadowany i generuje tekst", "cmd",
     f"kubectl -n {NS} exec deploy/triton-server -c triton-server -- sh -c \""
     "curl -s localhost:8000/v2/health/ready -o /dev/null -w 'server ready: HTTP %{http_code}\\n'; "
     "curl -s localhost:8000/v2/models/finance-qwen/ready -o /dev/null -w 'model finance-qwen ready: HTTP %{http_code}\\n'; "
     "curl -s -X POST localhost:8000/v2/models/finance-qwen/generate "
     "-d '{\\\"text_input\\\":\\\"Analyze Q3 tech market earnings.\\\",\\\"parameters\\\":{\\\"stream\\\":false,\\\"temperature\\\":0,\\\"max_tokens\\\":24}}'; echo\"", {}),
    # ---- tests (numbering as in the task 2 report) ----------------------------------------------
    ("t01_deployment_quota", "Test 1: integralność wdrożenia i quota", "cmd",
     f"kubectl -n {NS} get pods,pvc && kubectl -n {NS} describe resourcequota inference-monitoring-quota | tail -9", {}),
    ("t02_security_audit", "Test 2: audyt bezpieczeństwa", "cmd", "./scripts/verify_security.sh", {}),
    ("t03_targets_cli", "Test 3: cele Prometheusa i Alertmanager", "cmd",
     "curl -s localhost:9090/api/v1/targets | " + PY_TARGETS +
     "; echo; curl -s localhost:9090/api/v1/alertmanagers | python3 -c \"import sys,json; "
     "print('alertmanagers:', len(json.load(sys.stdin)['data']['activeAlertmanagers']))\"", {}),
    ("t03_prometheus_targets", "Test 3: Prometheus UI, Status > Targets", "web", "http://localhost:9090/targets",
     {"size": (1500, 800)}),
    ("t45_load_test", "Testy 4 i 5: skok obciążenia, alert, hot-reload (Job w klastrze)", "cmd",
     "TEST_PROFILE=gpu LOADGEN_IMAGE=$(kubectl -n inference-monitoring get deploy mcp-agent -o jsonpath='{.spec.template.spec.containers[0].image}') "
     "./scripts/run_in_cluster_test.sh 2>&1 | grep -v REPORT_JSON | tail -22", {"timeout": 600}),
    ("t45_vllm_engine_log", "Testy 4 i 5: log silnika vLLM, kolejka i przeładowanie (Triton)", "cmd",
     f"kubectl -n {NS} logs deploy/triton-server -c triton-server | grep -E 'metrics.py:341|Initializing an LLM engine' "
     "| sed -E 's/Avg prompt throughput: [0-9.]+ tokens\\/s, //; s/, GPU KV cache usage.*//; s/Initializing an LLM engine.*/ENGINE (RE)INIT/' | tail -16", {}),
    ("t45_grafana_dashboard", "Testy 4 i 5: dashboard Grafany po teście", "web", "GRAFANA_DASHBOARD",
     {"size": (1600, 1000), "budget": 25000}),
    ("t45_prometheus_alerts", "Testy 4 i 5: alerty w Prometheusie", "web", "http://localhost:9090/alerts",
     {"size": (1500, 900)}),
    ("t45_alertmanager", "Testy 4 i 5: Alertmanager", "web", "http://localhost:9093/#/alerts", {"size": (1500, 700)}),
    ("t06_gitops_selfheal", "Test 6: GitOps, self-heal ArgoCD", "cmd",
     f"kubectl -n {NS} delete svc alertmanager-service; kubectl -n {NS} scale deploy/grafana --replicas=0; "
     f"sleep 25; kubectl -n {NS} get svc alertmanager-service; kubectl -n {NS} get deploy grafana; "
     "kubectl -n argocd get application inference-monitoring-app", {"timeout": 120}),
    ("t07_operator_targets", "Test 7: profil operatora, ServiceMonitory", "cmd",
     "curl -s localhost:19090/api/v1/targets | " + PY_OP_TARGETS +
     "; curl -s localhost:19090/api/v1/rules | python3 -c \"import sys,json; "
     "[print('reguly:', g['name'], [r['name'] for r in g['rules']]) for g in json.load(sys.stdin)['data']['groups'] "
     "if g['name']=='inference_alerts']\"", {}),
    ("t08_github_actions", "Test 8: GitHub Actions, lista przebiegów", "web",
     "https://github.com/Shengan01/Inference-Server/actions", {"size": (1400, 1000), "budget": 20000}),
    ("t08_github_actions_run", "Test 8: GitHub Actions, szczegóły przebiegu (5 jobów)", "web", "LATEST_RUN",
     {"size": (1400, 1000), "budget": 20000}),
]


def sh(cmd, timeout=120, check=False):
    return subprocess.run(["bash", "-c", cmd], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=check)


def chrome():
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        p = sh(f"command -v {name}").stdout.strip()
        if p:
            return p
    sys.exit("Chrome not found")


CHROME = chrome()


def screenshot(src, out, size, budget=15000, extra=()):
    w, h = size
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                    f"--window-size={w},{h}", f"--virtual-time-budget={budget}", f"--screenshot={out}", *extra, src],
                   capture_output=True, text=True, timeout=180)


def render_terminal(step_id, title, command, output):
    lines = [f"$ {command}"] + output.rstrip("\n").split("\n")
    # wrap very long commands for display only
    shown = []
    for ln in lines:
        while len(ln) > 190:
            shown.append(ln[:190])
            ln = "  " + ln[190:]
        shown.append(ln)
    width = min(1900, max(900, int(max(len(x) for x in shown) * 8.1) + 48))
    height = len(shown) * 19 + 78
    body = "\n".join(
        (f'<span class="p">{html.escape(x)}</span>' if i == 0 else html.escape(x)) for i, x in enumerate(shown))
    page = f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:#0d1117}}
.bar{{background:#161b22;color:#8b949e;font:12px 'DejaVu Sans',sans-serif;padding:8px 14px;border-bottom:1px solid #30363d}}
pre{{margin:0;padding:12px 16px;color:#e6edf3;font:13px/19px 'DejaVu Sans Mono',monospace;white-space:pre}}
.p{{color:#7ee787}}</style><div class="bar">{html.escape(title)}</div><pre>{body}</pre>"""
    tmp = IMG / f"_{step_id}.html"
    tmp.write_text(page, encoding="utf-8")
    screenshot(tmp.as_uri(), IMG / f"{step_id}.png", (width, height), budget=3000)
    tmp.unlink()


def grafana_password():
    return sh(f"kubectl -n {NS} get secret grafana-secret -o jsonpath='{{.data.admin-password}}' | base64 -d").stdout


def grafana_screenshot(out, size=(1600, 1000), wait=14):
    """Log in to Grafana via its API and screenshot the dashboard through Chrome DevTools.

    The login form cannot be driven by `chrome --screenshot`, so the session cookie
    obtained from the API is injected into a headless Chrome over the DevTools protocol.
    Requires: pip install websockets
    """
    from websockets.sync.client import connect
    import urllib.request

    ip = sh("minikube ip").stdout.strip()
    pw = grafana_password()
    jar = IMG / "_cookies.txt"
    sh(f"curl -sk --resolve {HOST}:443:{ip} -c '{jar}' -H 'Content-Type: application/json' "
       f"-d '{{\"user\":\"admin\",\"password\":\"{pw}\"}}' https://{HOST}/grafana/login -o /dev/null")
    session = re.search(r"grafana_session\s+(\S+)", jar.read_text()).group(1)
    jar.unlink()
    found = sh(f"curl -sk --resolve {HOST}:443:{ip} -u admin:{pw} 'https://{HOST}/grafana/api/search?query=Inference'").stdout
    uid = re.search(r'"uid":"([^"]+)"', found).group(1)
    url = f"https://{HOST}/grafana/d/{uid}?orgId=1&from=now-4m&to=now&kiosk"

    port = 9333
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                             f"--remote-debugging-port={port}", f"--window-size={size[0]},{size[1]}",
                             f"--host-resolver-rules=MAP {HOST} {ip}", "--ignore-certificate-errors", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.5)
        with connect(ws_url, max_size=None) as ws:
            seq = [0]

            def call(method, **params):
                seq[0] += 1
                ws.send(json.dumps({"id": seq[0], "method": method, "params": params}))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == seq[0]:
                        return msg.get("result", {})

            call("Network.enable")
            call("Network.setCookie", name="grafana_session", value=session, domain=HOST, path="/",
                 secure=True, httpOnly=True)
            call("Emulation.setDeviceMetricsOverride", width=size[0], height=size[1], deviceScaleFactor=1, mobile=False)
            call("Page.enable")
            call("Page.navigate", url=url)
            time.sleep(wait)
            data = call("Page.captureScreenshot", format="png")["data"]
            Path(out).write_bytes(base64.b64decode(data))
    finally:
        proc.terminate()


def resolve_url(payload):
    ip = sh("minikube ip").stdout.strip()
    extra = []
    if payload == "LATEST_RUN":
        runs = sh("curl -s 'https://api.github.com/repos/Shengan01/Inference-Server/actions/runs?per_page=1&status=success'").stdout
        run_id = re.search(r'"id": *(\d+)', runs).group(1)
        return f"https://github.com/Shengan01/Inference-Server/actions/runs/{run_id}", extra
    return payload, extra


def main():
    wanted = set(sys.argv[1:])
    IMG.mkdir(parents=True, exist_ok=True)
    forwards = [
        sh(f"kubectl -n {NS} port-forward svc/prometheus-service 9090:9090 >/dev/null 2>&1 & echo $!").stdout.strip(),
        sh(f"kubectl -n {NS} port-forward svc/alertmanager-service 9093:9093 >/dev/null 2>&1 & echo $!").stdout.strip(),
        sh("kubectl -n monitoring port-forward svc/kps-prometheus 19090:9090 >/dev/null 2>&1 & echo $!").stdout.strip(),
    ]
    time.sleep(5)
    try:
        for step_id, title, kind, payload, opt in STEPS:
            if wanted and step_id not in wanted:
                continue
            print(f"[{step_id}] {title}", flush=True)
            if kind == "cmd":
                res = sh(payload, timeout=opt.get("timeout", 120))
                render_terminal(step_id, title, payload, res.stdout + res.stderr)
            elif payload == "GRAFANA_DASHBOARD":
                grafana_screenshot(IMG / f"{step_id}.png", opt["size"])
            else:
                url, extra = resolve_url(payload)
                screenshot(url, IMG / f"{step_id}.png", opt["size"], opt.get("budget", 15000), extra)
            print(f"    -> docs/img/{step_id}.png", flush=True)
    finally:
        for pid in forwards:
            sh(f"kill {pid} 2>/dev/null")
    return 0


if __name__ == "__main__":
    sys.exit(main())

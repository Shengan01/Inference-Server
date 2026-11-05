# Zadanie Cząstkowe 3: Implementacja w Kubernetes i Sprawozdanie z Realizacji Testów

**Przedmiot:** Bezpieczeństwo CI/CD / Zaawansowane Środowiska Chmurowe i Konteneryzacja
**Temat:** 3 – System zbierania metryk i monitoringu (Prometheus + Grafana + własny eksporter w Pythonie)
**Data wykonania zrzutów i testów:** 2026-11-05 (Minikube v1.35 + GitHub Actions)

## Linki do projektu (repozytorium publiczne)

| Zasób | Adres |
| :--- | :--- |
| **B. Repozytorium z manifestami** | https://github.com/Shengan01/Inference-Server |
| Manifesty Kubernetes (`base`, `overlays`, `addons`, `gitops`, `tests`, `examples`) | https://github.com/Shengan01/Inference-Server/tree/main/k8s |
| README repozytorium | https://github.com/Shengan01/Inference-Server/blob/main/README.md |
| GitHub Actions (potok CI/CD, wszystkie przebiegi) | https://github.com/Shengan01/Inference-Server/actions |
| Definicja potoku | https://github.com/Shengan01/Inference-Server/blob/main/.github/workflows/ci-cd.yaml |
| Przykładowy przebieg z raportem testów (artefakt `test-report`) | https://github.com/Shengan01/Inference-Server/actions/runs/41239646305 |
| Obraz kontenera (GitHub Container Registry) | https://github.com/Shengan01/Inference-Server/pkgs/container/mcp-agent |
| Instrukcja uruchomienia i testów | https://github.com/Shengan01/Inference-Server/blob/main/docs/INSTRUKCJA_URUCHOMIENIA_I_TESTOW.md |

**Zawartość sprawozdania.** Część **A** (rozdz. 1–3): potwierdzenie wdrożenia w klastrze (polecenia + zrzuty), prezentacja testów z Zadania 2 (cel, polecenia, zrzuty, wyniki) oraz komentarz do funkcjonalności. Część **B** (rozdz. 4): link do repozytorium z manifestami i opis jego zawartości. Zrzuty z terminala przedstawiają rzeczywisty wynik poleceń uruchomionych na klastrze (generowane skryptem `scripts/capture_evidence.py`), a zrzuty interfejsów (Prometheus, Grafana, Alertmanager, GitHub Actions) są zrzutami z przeglądarki.

---

# Część A

## 1. Potwierdzenie wdrożenia w klastrze Kubernetes

**Środowisko:** jednowęzłowy klaster Minikube (sterownik Docker, Kubernetes v1.35), namespace `inference-monitoring`. System wdrożono dwiema równoważnymi ścieżkami: skryptem lokalnym oraz GitOps (ArgoCD synchronizuje klaster z repozytorium).

```bash
./scripts/deploy_minikube.sh cpu-demo                      # wdrożenie lokalne (Kustomize, overlay local-cpu)
kubectl apply -f k8s/gitops/argocd-application.yaml        # wdrożenie GitOps (overlay cpu-demo z gałęzi main)
```

### 1.1. Mapa wymaganych obiektów

| Obiekt | Konfiguracja w projekcie | Manifest | Zrzut |
| :--- | :--- | :--- | :---: |
| **StatefulSet** | `prometheus`: stała tożsamość, `volumeClaimTemplates` (2Gi), retencja 15 dni, probes, `securityContext` | `k8s/base/prometheus/statefulset.yaml` | 1.3 |
| **Deployment** | `grafana`, `alertmanager`, `mcp-agent` (eksporter), `triton-server` (mock); probes, strategia `Recreate` dla Grafany (PVC RWO) | `k8s/base/*/deployment.yaml`, `k8s/overlays/cpu-demo/` | 1.3 |
| **Service** | 5 × `ClusterIP` (Prometheus, Alertmanager, Grafana, Triton, MCP Agent), nazwane porty | `k8s/base/*/service.yaml` | 1.4 |
| **Ingress** | NGINX, TLS, host `inference.local`; ścieżki `/grafana`, `/v2`, `/mcp` (rewrite) | `k8s/base/ingress/ingress.yaml` | 1.4 |
| **PVC** | `prometheus-storage-prometheus-0` 2Gi (z szablonu StatefulSet), `grafana-storage` 1Gi | `k8s/base/grafana/pvc.yaml`, StatefulSet | 1.5 |
| **ConfigMap** | konfiguracja Prometheusa i Alertmanagera, reguły alertów, datasource i dashboard Grafany, konfiguracja modelu | `k8s/base/*/configmap*.yaml` | 1.6 |
| **Secret** | `grafana-secret` (Opaque), `inference-tls` (`kubernetes.io/tls`); tworzone skryptem, bez wartości w Git; wzór w repozytorium | `scripts/bootstrap_secrets.sh`, `k8s/examples/secrets.example.yaml` | 1.6 |
| **NetworkPolicy** | `default-deny-all` + 6 reguł dla każdej pary komunikujących się usług | `k8s/base/network-policy.yaml` | 1.7 |
| **Requests i limits** | jawne `requests` i `limits` CPU/pamięć w każdym kontenerze (także init-container) | manifesty workloadów | 1.8 |
| **ResourceQuota** | 4 CPU / 8Gi (requests), 8 CPU / 16Gi (limits), 5 PVC, 15 podów | `k8s/base/resource-quota.yaml` | 1.8 |
| **LimitRange** | domyślne requests/limits kontenera oraz zakres 500Mi–10Gi dla PVC | `k8s/base/resource-quota.yaml` | 1.8 |

### 1.2. Klaster i namespace

```bash
kubectl get nodes -o wide && kubectl get namespaces
```
![Klaster i namespace](img/a01_cluster.png)

### 1.3. Deployment i StatefulSet

```bash
kubectl -n inference-monitoring get deployments,statefulsets,pods
```
![Deployment, StatefulSet, Pod](img/a02_workloads.png)

Komentarz: Prometheus działa jako StatefulSet (dane szeregów czasowych na trwałym wolumenie), pozostałe komponenty jako Deployment. Wszystkie pody są w stanie `Running 1/1`. Restarty wynikają z zatrzymania i ponownego uruchomienia Minikube przed wykonaniem zrzutów.

### 1.4. Service i Ingress

```bash
kubectl -n inference-monitoring get services,ingress
IP=$(minikube ip); curl -sk --resolve inference.local:443:$IP https://inference.local/grafana/api/health
```
![Service i Ingress](img/a03_services_ingress.png)

Dostęp zewnętrzny przez Ingress (TLS, kody odpowiedzi i przekierowanie HTTP → HTTPS):

![Dostęp przez Ingress](img/a10_ingress_access.png)

### 1.5. PersistentVolumeClaim

```bash
kubectl -n inference-monitoring get pvc && kubectl get pv
```
![PVC i PV](img/a04_storage.png)

### 1.6. ConfigMap i Secret

```bash
kubectl -n inference-monitoring get configmaps,secrets
```
![ConfigMap i Secret](img/a05_config_secrets.png)

Komentarz: wartości sekretów celowo nie są pokazywane ani przechowywane w Git. `grafana-secret` zawiera losowe hasło administratora, `inference-tls` certyfikat self-signed dla `inference.local`; oba tworzy `scripts/bootstrap_secrets.sh`. Wzór obiektów (z wartościami zastępczymi) znajduje się w `k8s/examples/secrets.example.yaml`.

### 1.7. NetworkPolicy

```bash
kubectl -n inference-monitoring get networkpolicies
```
![NetworkPolicy](img/a06_networkpolicy.png)

Komentarz: `default-deny-all` blokuje cały ruch w namespace, a pozostałe polityki otwierają wyłącznie wymagane połączenia (Prometheus → Triton/agent/Alertmanager, Grafana → Prometheus, agent → Prometheus/Triton, ruch z Ingress do usług, DNS). Polityki `loadgen-*` pochodzą z Jobu testowego (rozdz. 2).

### 1.8. Requests, limits, ResourceQuota i LimitRange

```bash
kubectl -n inference-monitoring get pods -o custom-columns=POD:.metadata.name,CONTAINER:.spec.containers[*].name,CPU_REQ:.spec.containers[*].resources.requests.cpu,CPU_LIM:.spec.containers[*].resources.limits.cpu,MEM_REQ:.spec.containers[*].resources.requests.memory,MEM_LIM:.spec.containers[*].resources.limits.memory
kubectl -n inference-monitoring describe resourcequota inference-monitoring-quota
kubectl -n inference-monitoring describe limitrange inference-monitoring-limits
```
![Requests i limits](img/a09_requests_limits.png)

![ResourceQuota](img/a07_resourcequota.png)

![LimitRange](img/a08_limitrange.png)

### 1.9. GitOps i profil operatora

```bash
kubectl -n argocd get applications && kubectl -n monitoring get pods
```
![ArgoCD i profil operatora](img/a11_gitops.png)

### 1.10. Dodatkowe obiekty i zasoby

| Obiekt | Uzasadnienie |
| :--- | :--- |
| `ServiceAccount` ×5, `ClusterRole` + `ClusterRoleBinding` | RBAC najmniejszych uprawnień: osobne konta dla każdego komponentu, Prometheus tylko do odczytu (bez dostępu do Secrets) |
| `Namespace` z etykietami Pod Security | `enforce: baseline`, `audit`/`warn: restricted` |
| `securityContext` + probes | `runAsNonRoot`, `drop: ALL`, `seccomp: RuntimeDefault`, `readinessProbe`/`livenessProbe` w każdym komponencie |
| `Job` + `NetworkPolicy` testowe | generator obciążenia działa wewnątrz klastra (bez narzutu `port-forward`) |
| `ServiceMonitor` ×2, `PrometheusRule` (CRD) | profil Prometheus Operator, wykrywanie celów przez zasoby Kubernetes |
| Helm (`kube-prometheus-stack`) | gotowy stos operatora wdrażany z własnymi wartościami |
| ArgoCD `Application` ×3 | GitOps: automatyczna synchronizacja, `prune`, `selfHeal` |
| Kustomize (`base`, 5 overlayów) | jedna baza, profile `cpu-demo`/`gpu`/`local-*` |
| GitHub Actions + Trivy + kubeconform | walidacja i skan manifestów oraz obrazu, test na `kind`, automatyczne wdrożenie obrazu |

## 2. Prezentacja testów

Testy odpowiadają procedurze zaplanowanej w Zadaniu 2 (`docs/ZADANIE_2_OPIS_I_ARCHITEKTURA.md`, rozdz. 3, testy 1–8). Poniżej: cel (lub odniesienie), użyte polecenia, zrzut i wynik.

### Test 1: integralność wdrożenia i quota
**Cel:** wszystkie obciążenia działają, PVC są `Bound`, zużycie mieści się w quota (Zadanie 2, Test 1).
```bash
kubectl -n inference-monitoring get pods,pvc
kubectl -n inference-monitoring describe resourcequota inference-monitoring-quota | tail -9
```
![Test 1](img/t01_deployment_quota.png)
**Wynik:** 5 podów `Running 1/1`, oba PVC `Bound`, zużycie quota poniżej limitów.

### Test 2: audyt bezpieczeństwa (RBAC, NetworkPolicy, non-root)
**Cel:** uprawnienia najmniejszego zakresu i bezpieczne kontenery (Zadanie 2, Test 2).
```bash
./scripts/verify_security.sh
```
![Test 2](img/t02_security_audit.png)
**Wynik:** `prometheus-sa` może czytać pody (`yes`), nie może ich usuwać (`no`) ani czytać Secrets (`no`); wszystkie pody mają `NonRoot: true`; polityki sieciowe, ResourceQuota i LimitRange są aktywne.

### Test 3: potok metryk i wizualizacji
**Cel:** Prometheus zbiera metryki z Tritona i eksportera, Alertmanager jest podłączony, Grafana ma wczytane źródło danych i dashboard (Zadanie 2, Test 3).
```bash
curl -s localhost:9090/api/v1/targets | python3 -c "..."      # cele i ich stan
curl -s localhost:9090/api/v1/alertmanagers
```
![Test 3, cele z linii poleceń](img/t03_targets_cli.png)
Interfejs Prometheusa (Status → Targets):
![Test 3, Prometheus UI](img/t03_prometheus_targets.png)
**Wynik:** Cele `prometheus`, `triton` i `mcp_agent_config` mają stan `up`, Alertmanager jest podłączony.

### Testy 4 i 5: skok obciążenia, alert, hot-reload i powrót do normy
**Cel:** pod wpływem obciążenia rośnie opóźnienie i uruchamia się alert; agent SRE zmienia `max_num_seqs` z 32 na 512 bez przestoju, a opóźnienie wraca do wartości bazowej (Zadanie 2, Testy 4 i 5). Test działa jako Job w klastrze.
```bash
LOADGEN_IMAGE=<obraz mcp-agent> ./scripts/run_in_cluster_test.sh
```
![Testy 4 i 5, wynik](img/t45_load_test.png)

| Faza | Żądań/s | Wysłane | Pomyślne | Opóźnienie serwera [ms] |
| :--- | :---: | :---: | :---: | :---: |
| 1. CALM | 20 | 200 | 200 | 15.8 |
| 2. STORM | 400 | 6000 | 6000 | **202.1** |
| 3. RESOLUTION (po hot-reload) | 400 | 6000 | 6000 | **14.2** |

Dashboard Grafany po teście (dwa kolejne przebiegi testu: skok ruchu i opóźnienia, następnie powrót do normy; `max_num_seqs` = 512 po hot-reload):
![Dashboard Grafany](img/t45_grafana_dashboard.png)
Alerty w Prometheusie i w Alertmanagerze:
![Alerty w Prometheusie](img/t45_prometheus_alerts.png)
![Alertmanager](img/t45_alertmanager.png)
**Wynik:** wszystkie 7 kryteriów spełnionych. Skuteczność żądań 100%, opóźnienie serwera rośnie w burzy ponad 12.8× względem bazowego i wraca do poziomu bazowego po hot-reload; aktywny alert: HighInferenceQueueDepth. Opóźnienie liczone jest z liczników Prometheusa (`Δ nv_inference_request_duration_us / Δ nv_inference_request_success`), czyli tak samo jak w regule alertu.

### Test 6: GitOps, self-heal ArgoCD
**Cel:** ręczne zmiany w klastrze są cofane do stanu z repozytorium (Zadanie 2, Test 6).
```bash
kubectl -n inference-monitoring delete svc alertmanager-service
kubectl -n inference-monitoring scale deploy/grafana --replicas=0
sleep 25; kubectl -n inference-monitoring get svc alertmanager-service; kubectl -n inference-monitoring get deploy grafana
kubectl -n argocd get application inference-monitoring-app
```
![Test 6](img/t06_gitops_selfheal.png)
**Wynik:** Usunięty Service i wyzerowane repliki Grafany zostały przywrócone przez ArgoCD, aplikacja ma stan `Synced`.

### Test 7: profil Prometheus Operator (ServiceMonitor, Helm)
**Cel:** Prometheus z chartu `kube-prometheus-stack` wykrywa cele przez ServiceMonitory i ładuje reguły z `PrometheusRule` (Zadanie 2, Test 7).
```bash
kubectl -n monitoring port-forward svc/kps-prometheus 19090:9090 &
curl -s localhost:19090/api/v1/targets | python3 -c "..."      # cele inference-monitoring
curl -s localhost:19090/api/v1/rules | python3 -c "..."        # grupa inference_alerts
```
![Test 7](img/t07_operator_targets.png)
**Wynik:** Oba ServiceMonitory (`triton`, `mcp-agent-config`) mają stan `up`, grupa `inference_alerts` zawiera 4 reguły.

### Test 8: potok CI/CD (GitHub Actions)
**Cel:** każdy push uruchamia walidację, skan, test w klastrze, zbudowanie obrazu i aktualizację wdrożenia (Zadanie 2, Test 8).
```text
https://github.com/Shengan01/Inference-Server/actions
```
![Test 8, lista przebiegów](img/t08_github_actions.png)
![Test 8, szczegóły przebiegu](img/t08_github_actions_run.png)
**Wynik:** Przebiegi na gałęzi `main` kończą się sukcesem (5 jobów: walidacja manifestów, skan bezpieczeństwa, test na `kind`, zbudowanie i skan obrazu, aktualizacja tagu w Git).

## 3. Komentarz do funkcjonalności (zakładana i osiągnięta)

**Założenia.** Zbudować w Kubernetes system obserwowalności (Prometheus + Grafana + własny eksporter) dla serwera inferencji, z alertami, bezpiecznym dostępem, automatyzacją CI/CD i GitOps. Model nie jest przedmiotem oceny, jest obciążeniem generującym metryki.

**Osiągnięte.** Wszystkie wymagane obiekty (rozdz. 1.1) są skonfigurowane i działają, a testy 1–8 kończą się wynikiem pozytywnym. System reaguje na przeciążenie (alert), a agent SRE przywraca opóźnienie do wartości bazowej bez przestoju. Dodatkowo wdrożono profil Prometheus Operator, GitOps i potok CI/CD z testem na `kind`.

**Ograniczenia (uczciwie):**
* Wdrożenie i testy wykonano na profilu `cpu-demo`, w którym serwer inferencji jest **mockiem Tritona** (emuluje API i metryki; nie wykorzystuje GPU). Profil `gpu` (prawdziwy Triton z vLLM) nie był jeszcze uruchamiany. Wartości bezwzględne opóźnień zależą od modelu mocka i mocy maszyny; istotny jest kierunek zmiany i powrót do normy.
* Opóźnienie po stronie klienta (kilkaset ms przy 400 równoczesnych żądaniach) zależy od mocy generatora i nie opisuje usługi, dlatego w wynikach podano opóźnienie z Prometheusa.
* Alertmanager nie ma skonfigurowanego odbiorcy (e-mail/webhook); alerty są widoczne w jego interfejsie. Reguła `HighInferenceLatency` nie jest wymagana przez żaden test; w teście aktywny jest `HighInferenceQueueDepth`.
* TLS jest self-signed, a sekrety tworzone są skryptem (poza Git). W środowisku produkcyjnym należy użyć cert-manager i Sealed/External Secrets.
* W Minikube kontroler Ingress jest usługą `NodePort` pod `minikube ip` (w chmurze otrzymałby adres LoadBalancera).
* Podczas wykonywania zrzutów wykryto i poprawiono niezgodność mocka z prawdziwym Triton (`nv_gpu_utilization` jako ułamek 0–1 oraz brakująca metryka `nv_inference_pending_request_count`), która powodowała błędne wartości na dashboardzie.

---

# Część B

## 4. Repozytorium z manifestami

**Repozytorium:** https://github.com/Shengan01/Inference-Server (publiczne, gałąź `main`)

Plik `README.md` w repozytorium opisuje architekturę, szybki start, strukturę katalogów, metryki i alerty oraz zawiera spis dokumentacji (w tym instrukcję uruchomienia i testów oraz wersje PDF w `docs/pdf/`).

| Ścieżka | Zawartość |
| :--- | :--- |
| `k8s/base/` | wspólne manifesty: namespace, RBAC, ResourceQuota/LimitRange, NetworkPolicy, Prometheus (StatefulSet), Grafana, Alertmanager, Ingress, agent |
| `k8s/overlays/` | profile Kustomize: `cpu-demo`, `gpu`, `local-cpu`, `local-gpu` |
| `k8s/addons/operator-stack/` | ServiceMonitory, PrometheusRule, NetworkPolicy i wartości Helm (profil operatora) |
| `k8s/gitops/` | aplikacje ArgoCD |
| `k8s/tests/`, `k8s/examples/` | Job generatora obciążenia z NetworkPolicy; wzorcowe manifesty Secret |
| `mcp_agent/` | własny eksporter metryk i kontroler (Python, FastMCP + FastAPI) |
| `scripts/` | wdrożenie, sekrety, audyt bezpieczeństwa, test w klastrze, zrzuty, PDF |
| `.github/workflows/ci-cd.yaml` | potok CI/CD |
| `docs/` | dokumentacja i sprawozdania (Zadanie 2 i 3, dokumentacja techniczna, instrukcja) oraz `docs/pdf/` |

Odtworzenie wdrożenia (szczegóły w instrukcji):
```bash
git clone https://github.com/Shengan01/Inference-Server.git && cd Inference-Server
./scripts/deploy_minikube.sh cpu-demo
LOADGEN_IMAGE=mcp-agent:local ./scripts/run_in_cluster_test.sh
```

---

## Załącznik: architektura systemu

**Architektura klastra:**

```mermaid
flowchart LR
    classDef client fill:#1e293b,stroke:#64748b,stroke-width:2px,color:#ffffff;
    classDef k8s fill:#0f3b6d,stroke:#38bdf8,stroke-width:2px,color:#ffffff;
    classDef obs fill:#0e462a,stroke:#4ade80,stroke-width:2px,color:#ffffff;
    classDef sec fill:#5c1d24,stroke:#f87171,stroke-width:2px,color:#ffffff;
    classDef storage fill:#522e0a,stroke:#fbbf24,stroke-width:2px,color:#ffffff;

    USER["Inference clients<br/>test load"]:::client
    ADMIN["SRE and Operations"]:::client
    LB["Entry point: minikube IP, NodePort<br/>cloud: LoadBalancer<br/>inference.local, TLS"]:::k8s
    ING["NGINX Ingress Controller"]:::k8s

    subgraph Cluster ["Namespace: inference-monitoring"]
        direction TB
        subgraph Workloads ["Inference and control workloads"]
            direction LR
            TRITON["Triton Inference Server<br/>Deployment :8000 / :8002<br/>mock on cpu-demo"]:::k8s
            AGENT["MCP Agent and exporter<br/>Deployment, Python :5000"]:::k8s
        end
        subgraph Observability ["Observability layer"]
            direction LR
            PROM["Prometheus<br/>StatefulSet :9090"]:::obs
            RULES["Alerting rules<br/>ConfigMap, 4 alerts"]:::obs
            AM["Alertmanager<br/>Deployment :9093"]:::obs
            GRAF["Grafana<br/>Deployment :3000 /grafana"]:::obs
            SM["ServiceMonitors, PrometheusRule<br/>addons/operator-stack"]:::obs
        end
        subgraph State ["State and configuration"]
            direction LR
            PVC1[("PVC prometheus-storage 2Gi")]:::storage
            PVC2[("PVC grafana-storage 1Gi")]:::storage
            CM["ConfigMaps<br/>dashboard, datasource, model config"]:::storage
            SEC["Secrets created out of band<br/>grafana-secret, inference-tls"]:::storage
        end
        subgraph Security ["Security controls"]
            direction LR
            NP["NetworkPolicies<br/>default-deny + allow rules"]:::sec
            QUOTA["ResourceQuota, LimitRange<br/>4 CPU, 8Gi RAM"]:::sec
        end
    end

    CR["ClusterRole prometheus-reader-role<br/>cluster-scoped, read-only"]:::sec

    subgraph Operator ["Namespace: monitoring (optional operator profile, Helm)"]
        KPS["kube-prometheus-stack<br/>Operator, Prometheus,<br/>Alertmanager, Grafana"]:::obs
    end

    USER -->|HTTPS /v2| LB
    ADMIN -->|HTTPS /grafana, /mcp| LB
    LB --> ING
    ING -->|/v2| TRITON
    ING -->|/mcp| AGENT
    ING -->|/grafana| GRAF

    PROM -->|scrape :8002| TRITON
    PROM -->|scrape :5000/metrics/config| AGENT
    PROM -.->|evaluate| RULES
    PROM -->|firing alerts| AM
    GRAF -->|PromQL| PROM
    AGENT -->|PromQL| PROM
    AGENT -->|hot-reload :8000| TRITON

    PROM --> PVC1
    GRAF --> PVC2
    CM -.-> GRAF
    CM -.-> AGENT
    SEC -.-> GRAF
    SEC -.-> ING
    CR -.->|bound to prometheus-sa| PROM
    KPS -.->|discovers| SM
    KPS -.->|scrape| TRITON
    KPS -.->|scrape| AGENT

    style Cluster fill:#eef2f7,stroke:#475569,stroke-width:2px,color:#111111
    style Workloads fill:#e0f2fe,stroke:#38bdf8,color:#111111
    style Observability fill:#dcfce7,stroke:#4ade80,color:#111111
    style State fill:#fef3c7,stroke:#fbbf24,color:#111111
    style Security fill:#fee2e2,stroke:#f87171,color:#111111
    style Operator fill:#dcfce7,stroke:#4ade80,color:#111111
```

**Dostarczanie: CI/CD i GitOps:**

```mermaid
flowchart LR
    classDef client fill:#1e293b,stroke:#64748b,stroke-width:2px,color:#ffffff;
    classDef k8s fill:#0f3b6d,stroke:#38bdf8,stroke-width:2px,color:#ffffff;
    classDef cicd fill:#3b1d5c,stroke:#c084fc,stroke-width:2px,color:#ffffff;
    classDef storage fill:#522e0a,stroke:#fbbf24,stroke-width:2px,color:#ffffff;
    classDef obs fill:#0e462a,stroke:#4ade80,stroke-width:2px,color:#ffffff;

    DEV["Developer<br/>git push to main"]:::client
    subgraph GHA ["GitHub Actions (5 jobs)"]
        direction TB
        J1["lint-and-validate<br/>yamllint, shellcheck, kubeconform"]:::cicd
        J2["security-audit<br/>Trivy IaC"]:::cicd
        J3["integration-test<br/>kind cluster, test Job"]:::cicd
        J4["build-scan-push<br/>build, Trivy image, push"]:::cicd
        J5["gitops-bump<br/>commit new image tag"]:::cicd
        J1 --> J2
        J1 --> J3
        J2 --> J4
        J3 --> J4
        J4 --> J5
    end
    GHCR[("ghcr.io<br/>mcp-agent image")]:::storage
    GIT["Git repository main<br/>k8s/overlays, image tag"]:::cicd
    ARGO["ArgoCD<br/>automated sync, prune, self-heal"]:::cicd
    APP1["inference-monitoring-app<br/>namespace inference-monitoring"]:::k8s
    APP2["kube-prometheus-stack and<br/>operator-stack-resources<br/>namespace monitoring"]:::obs

    DEV --> J1
    J4 -->|push image| GHCR
    J5 -->|commit tag| GIT
    ARGO -->|poll| GIT
    ARGO --> APP1
    ARGO -->|Helm chart + addon| APP2
    GHCR -.->|image pull| APP1

    style GHA fill:#f3e8ff,stroke:#c084fc,color:#111111
```

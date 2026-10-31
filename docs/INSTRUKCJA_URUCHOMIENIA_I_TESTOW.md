# Instrukcja uruchomienia i testowania

Instrukcja krok po kroku: jak wdrożyć system na lokalnym klastrze Minikube, jak go przetestować, jak uruchomić wariant GitOps (ArgoCD) oraz profil Prometheus Operator. Wszystkie polecenia wykonuje się z katalogu głównego repozytorium.

## 1. Wymagania

| Narzędzie | Do czego | Sprawdzenie |
| :--- | :--- | :--- |
| Docker | sterownik Minikube i budowa obrazu | `docker version` |
| Minikube | lokalny klaster Kubernetes | `minikube version` |
| kubectl (v1.28+) | obsługa klastra, `kubectl kustomize` | `kubectl version --client` |
| Helm (tylko profil operatora) | instalacja `kube-prometheus-stack` | `helm version` |
| Python 3 + `httpx` (tylko test lokalny poza klastrem) | skrypt testowy | `python3 -c "import httpx"` |

Zalecane zasoby Minikube: 4 CPU i 6 GB RAM dla samego stosu; 6 CPU i 12 GB RAM, jeśli dodatkowo działają ArgoCD i profil operatora.

```bash
minikube start --driver=docker --cpus=6 --memory=12288
```

## 2. Wdrożenie (jedno polecenie)

```bash
./scripts/deploy_minikube.sh cpu-demo        # profil demonstracyjny (mock Triton, bez GPU)
# ./scripts/deploy_minikube.sh gpu           # klaster z kartą NVIDIA
```

Skrypt: uruchamia Minikube (jeśli nie działa), włącza dodatki `ingress` i `metrics-server`, buduje obraz `mcp-agent:local`, tworzy sekrety (`scripts/bootstrap_secrets.sh`), stosuje overlay `k8s/overlays/local-cpu` i czeka na gotowość podów.

> **Uwaga:** zaraz po włączeniu dodatku `ingress` kontroler potrzebuje chwili. Jeśli `kubectl apply` zgłosi błąd webhooka `validate.nginx.ingress.kubernetes.io ... connection refused`, odczekaj i powtórz:
> `kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller && kubectl apply -k k8s/overlays/local-cpu`

Sprawdzenie:

```bash
kubectl -n inference-monitoring get pods,pvc,svc,ingress
```

Oczekiwane: 5 podów `Running` (`prometheus-0`, `grafana`, `alertmanager`, `mcp-agent`, `triton-server`), dwa PVC `Bound` (`prometheus-storage-prometheus-0` 2Gi, `grafana-storage` 1Gi).

## 3. Dostęp do usług

Ingress obsługuje host `inference.local` z TLS (certyfikat self-signed, przeglądarka pokaże ostrzeżenie).

```bash
echo "$(minikube ip) inference.local" | sudo tee -a /etc/hosts
# Minikube (sterownik docker na Linuksie): kontroler Ingress to usługa NodePort dostępna pod `minikube ip`.
# Gdy adres Minikube jest nieosiągalny z hosta (macOS/Windows), użyj w osobnym terminalu `minikube tunnel`
# i wpisu 127.0.0.1 w /etc/hosts.
```

| Usługa | Adres | Uwagi |
| :--- | :--- | :--- |
| Grafana | `https://inference.local/grafana/` | login `admin`, hasło poniżej |
| Triton (mock) | `https://inference.local/v2/health/ready` | |
| MCP Agent | `https://inference.local/mcp/health` | `/mcp/...` jest przepisywane na `/...` |
| Prometheus | `kubectl -n inference-monitoring port-forward svc/prometheus-service 9090:9090` | bez uwierzytelniania, dlatego bez Ingress |
| Alertmanager | `kubectl -n inference-monitoring port-forward svc/alertmanager-service 9093:9093` | j.w. |

Hasło Grafany jest losowe i nie leży w repozytorium:

```bash
kubectl -n inference-monitoring get secret grafana-secret -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

Skrót do wszystkich port-forwardów: `./scripts/start_port_forward.sh &`.

## 4. Testy

### 4.1. Audyt bezpieczeństwa (RBAC, non-root, NetworkPolicy, quota)

```bash
./scripts/verify_security.sh
```

Oczekiwane: `prometheus-sa` może czytać pody (`yes`), nie może ich usuwać (`no`) ani czytać Secrets (`no`); wszystkie pody mają `NonRoot: true`; wylistowane są NetworkPolicy, ResourceQuota i LimitRange; w manifestach nie ma jawnego hasła.

### 4.2. Test obciążeniowy, alertu i hot-reload (w klastrze)

```bash
LOADGEN_IMAGE=mcp-agent:local ./scripts/run_in_cluster_test.sh
```

Skrypt tworzy Job `loadgen` (z `NetworkPolicy` tylko do Tritona, agenta i Prometheusa), czeka na zakończenie, wypisuje log i zapisuje `test-output.log` oraz `test-report.json` (zmienna `OUT_DIR` zmienia katalog). Kod wyjścia jest różny od 0, jeśli którekolwiek kryterium nie przejdzie.

Fazy: **CALM** (20 żądań/s), **STORM** (seria 400 równoczesnych żądań na sekundę, zmienna `STORM_RPS`), rekonfiguracja `/reconfigure` (`max_num_seqs` 32 → 512, hot-reload), **RESOLUTION** (ten sam ruch). Kryteria PASS i interpretacja są w `docs/ZADANIE_3_IMPLEMENTACJA_I_SPRAWOZDANIE.md`, rozdz. 5.

Po teście możesz obejrzeć wykresy w Grafanie (dashboard *Autonomous Inference Cluster*) i aktywne alerty w Prometheusie (`/alerts`).

Powtarzalność: przed kolejnym uruchomieniem zresetuj stan mocka (wraca do `max_num_seqs` = 32):

```bash
kubectl -n inference-monitoring rollout restart deploy/triton-server
kubectl -n inference-monitoring rollout status deploy/triton-server
```

### 4.3. Walidacja manifestów lokalnie (bez klastra)

```bash
for d in k8s/base k8s/overlays/cpu-demo k8s/overlays/gpu k8s/overlays/local-cpu k8s/overlays/local-gpu k8s/addons/operator-stack; do
  kubectl kustomize "$d" > /dev/null && echo "$d OK"
done
```

To samo, plus `kubeconform`, `yamllint`, `shellcheck` i skan Trivy, wykonuje potok CI (rozdz. 7).

## 5. GitOps z ArgoCD (zadanie 4)

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server

./scripts/bootstrap_secrets.sh                         # sekrety muszą istnieć przed pierwszą synchronizacją
kubectl apply -f k8s/gitops/argocd-application.yaml
kubectl -n argocd get app inference-monitoring-app     # oczekiwane: Synced / Healthy
```

ArgoCD pobiera overlay `k8s/overlays/cpu-demo` z gałęzi `main` i obraz `ghcr.io/shengan01/mcp-agent` (pakiet jest publiczny).

Test self-heal:

```bash
kubectl -n inference-monitoring delete svc alertmanager-service
kubectl -n inference-monitoring scale deploy/grafana --replicas=0
kubectl -n inference-monitoring get svc alertmanager-service deploy/grafana    # po kilkudziesięciu sekundach oba wracają
```

Pełny przepływ: `git push` → GitHub Actions (testy, obraz, skan) → job `gitops-bump` zapisuje nowy tag obrazu w overlayach → ArgoCD wykrywa zmianę w Git i wdraża.

## 6. Profil Prometheus Operator (Helm, opcjonalny)

```bash
OPERATOR_PROFILE=true ./scripts/bootstrap_secrets.sh       # sekret grafana-admin w namespace monitoring
kubectl apply -f k8s/gitops/argocd-application-operator.yaml
kubectl -n argocd get app kube-prometheus-stack operator-stack-resources
```

Weryfikacja:

```bash
kubectl -n monitoring port-forward svc/kps-prometheus 19090:9090 &
curl -s localhost:19090/api/v1/targets | grep -o '"scrapePool":"[^"]*"'   # serviceMonitor/inference-monitoring/triton/0, .../mcp-agent-config/0
curl -s localhost:19090/api/v1/rules | grep -o 'inference_alerts'
```

Bez ArgoCD, ręcznie przez Helm:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  --version 58.2.2 -f k8s/addons/operator-stack/helm-values.yaml
kubectl apply -k k8s/addons/operator-stack
```

## 7. Potok CI/CD (GitHub Actions)

Plik `.github/workflows/ci-cd.yaml`, uruchamiany po każdym pushu i pull requeście na `main`:

| Job | Co robi |
| :--- | :--- |
| `lint-and-validate` | `yamllint`, `shellcheck`, kompilacja skryptów Pythona, `kubectl kustomize` każdego overlaya + `kubeconform` |
| `security-audit` | Trivy na manifestach (CRITICAL blokuje, HIGH idzie do SARIF) |
| `integration-test` | klaster `kind`, wdrożenie `local-cpu`, `verify_security.sh`, test w klastrze; artefakt `test-report` |
| `build-scan-push` (tylko `main`) | budowa obrazu, skan Trivy (naprawialne HIGH/CRITICAL blokują), push do `ghcr.io` |
| `gitops-bump` (tylko `main`) | zapis nowego tagu obrazu w Git (commit `[skip ci]`) |

Wynik: zakładka *Actions* w repozytorium. Raport z testu pobierzesz z artefaktu `test-report` danego przebiegu.

## 8. Sprzątanie

```bash
kubectl delete -f k8s/gitops/argocd-application.yaml                 # jeśli używano ArgoCD (usuwa zasoby przez finalizer)
kubectl delete -k k8s/overlays/local-cpu                             # wdrożenie bez ArgoCD
kubectl delete namespace monitoring argocd                           # profil operatora i ArgoCD
minikube stop                                                        # zatrzymanie klastra, dane zostają
```

## 9. Najczęstsze problemy

| Objaw | Przyczyna / rozwiązanie |
| :--- | :--- |
| Błąd webhooka `validate.nginx.ingress.kubernetes.io` przy `kubectl apply` | kontroler Ingress jeszcze nie wstał; poczekaj na `rollout status` i powtórz |
| `Grafana` zwraca 404 pod `/` | Grafana jest serwowana pod `/grafana/` |
| `curl: (6) Could not resolve host: inference.local` | brak wpisu w `/etc/hosts` (rozdz. 3) |
| Pod `mcp-agent` w `ImagePullBackOff` w profilu `cpu-demo` | pakiet `ghcr.io/shengan01/mcp-agent` musi być publiczny; lokalnie użyj overlaya `local-cpu` |
| Pody `Pending` z powodu quota | `kubectl -n inference-monitoring describe resourcequota` |
| Test STORM nie pokazuje degradacji | mock jest w stanie po rekonfiguracji (512); zrestartuj `triton-server` (rozdz. 4.2) |
| Grafana z chartu nie startuje | brak sekretu `grafana-admin`: `OPERATOR_PROFILE=true ./scripts/bootstrap_secrets.sh` |

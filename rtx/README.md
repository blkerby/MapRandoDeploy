AI-generated notes:

# RTX k3s deployment

Run a single k3s server on rtx, using its bundled containerd,
SQLite, and Traefik. ServiceLB is disabled and Traefik uses an internal
`ClusterIP` service. Bootstrap the node, verify CUDA, then deploy applications
and test TLS and ingress privately. OVHcloud/frp setup follows those checks;
the existing Google Cloud reverse SSH proxy stays separate. No installation or
DNS change is performed merely by editing these files.

## Versions and host settings

| Component | Pin |
| --- | --- |
| k3s | v1.36.5+k3s1 |
| NVIDIA Container Toolkit packages | 1.20.1-1 |
| NVIDIA device plugin chart | 0.20.1 |
| cert-manager chart | v1.21.2 |
| Traefik | Bundled with the k3s pin |

Node package pins live in `node/versions.env`; chart pins live in `infra/`.
The bootstrap keeps the existing GPU drivers and Docker installation. It writes
the k3s configuration before running the upstream installer, which starts the
service on initial installation. Later configuration changes and upgrades are
separate steps; rerunning the installer may skip restarting an unchanged binary.

The bootstrap installs the NVIDIA Container Toolkit and k3s using apt and the
upstream k3s installer. K3s detects the node address and hostname automatically.

The bootstrap leaves UFW configuration unchanged. Use SSH forwarding for private
administrator access and port-forwarding for web tests. Public web access will
use frp without home router port forwards. Verify cluster DNS, pod-to-pod
communication, and outbound connectivity after installation. Investigate any
failures before proposing firewall changes.

A separate, unapplied [outbound firewall configuration](node/firewall.md) is
available for restricting host and pod connections to the home LAN. It is not
part of bootstrap.

K3s manages the required kernel modules and networking sysctls. Its default
kubelet configuration allows host swap while preventing pods from using swap.
No custom CPU/RAM reservations are configured.

## Bootstrap the node

From this repository on the workstation:

```bash
rsync -av --exclude=k3s.yaml --exclude=secrets/ --exclude='*kubeconfig.yaml' \
  rtx/ rtx:~/MapRandoDeploy/rtx/
ssh -t rtx
```

On rtx, run:

```bash
cd ~/MapRandoDeploy/rtx
sudo ./node/bootstrap.sh
```

For a private administrator kubeconfig, on rtx:

```bash
install -d -m 0700 ~/.kube
sudo install -o kerby -g kerby -m 0600 \
  /etc/rancher/k3s/k3s.yaml /home/kerby/.kube/rtx.yaml
```

On the workstation, keep an SSH tunnel running in a separate terminal:

```bash
ssh -N -L 127.0.0.1:16443:127.0.0.1:6443 rtx
```

Then, from `MapRandoDeploy/rtx` on the workstation:

```bash
umask 077
scp rtx:~/.kube/rtx.yaml ./k3s.yaml
export KUBECONFIG="$PWD/k3s.yaml"
kubectl config rename-context default rtx
kubectl config set-cluster default --server=https://127.0.0.1:16443
kubectl get nodes -o wide
```

The kubeconfig grants cluster administrator access. It and `secrets/` are ignored
by Git. Replace copied administrator kubeconfigs when their client certificates
are renewed. Never reuse the old dbm kubeconfig or old CI credentials.

## Install infrastructure and verify CUDA

Run workstation commands below with the `rtx` kubeconfig selected:

```bash
kubectl apply -f infra/cert-manager.yaml -f infra/nvidia-device-plugin.yaml -f infra/traefik.yaml
kubectl -n cert-manager wait --for=create deployment/cert-manager --timeout=600s
kubectl -n cert-manager rollout status deployment/cert-manager --timeout=600s
kubectl -n cert-manager wait --for=create deployment/cert-manager-webhook --timeout=600s
kubectl -n cert-manager rollout status deployment/cert-manager-webhook --timeout=600s
kubectl -n cert-manager wait --for=create deployment/cert-manager-cainjector --timeout=600s
kubectl -n cert-manager rollout status deployment/cert-manager-cainjector --timeout=600s
kubectl -n nvidia-device-plugin wait --for=create daemonset/nvidia-device-plugin --timeout=600s
kubectl -n nvidia-device-plugin rollout status daemonset/nvidia-device-plugin --timeout=600s
kubectl -n kube-system rollout status deployment/traefik --timeout=600s
kubectl get node rtx -o jsonpath='{.status.allocatable.nvidia\.com/gpu\.shared}{"\n"}'
```

Expect eight allocatable shared slots: four per physical GPU across two GPUs.
HelmChart objects install the pinned charts through k3s's Helm controller; a
separate Helm CLI is unnecessary. If a chart fails,
inspect `kubectl -n kube-system get jobs,pods` and the corresponding
`helm-install-*` job logs. A chart with `failurePolicy: abort` requires manual
recovery after its installation error is fixed.

```bash
kubectl apply -f node/gpu-smoke-test.yaml
kubectl wait --for=condition=complete job/gpu-smoke-test --timeout=300s
kubectl logs job/gpu-smoke-test
kubectl delete job gpu-smoke-test
```

The CUDA vector-add program must succeed. Delete the job after inspection to
repeat it. This tests a CUDA computation as well as GPU device access.

Each `nvidia.com/gpu.shared: 1` allocation consumes one shared slot on a physical
GPU. Time-slicing permits concurrent workloads but does not enforce a 25% compute
or memory partition. The serving configuration separately sets
`cuda_memory_fraction: 0.25` to limit the process's PyTorch allocator; CUDA memory
outside that allocator still needs headroom. Verify concurrent startup and
serving under load before cutover.

Both GPUs offer shared slots; this configuration does not reserve a physical GPU
for host-side training. Host training is not accounted for by Kubernetes. Before
serving alongside training, inspect GPU UUIDs for all serving pods (for example,
`kubectl exec deploy/map-gen -- nvidia-smi -L`) and coordinate host training using
`CUDA_VISIBLE_DEVICES`. Recheck after pods are recreated, since Kubernetes may
allocate a different GPU. Inside each pod, `cuda:0` means its allocated GPU, not
necessarily host GPU index zero.

## Deploy the internal applications

Create these secrets in the `default` namespace using credentials from your
password manager or other private source:

| Secret | Required content |
| --- | --- |
| `dockerhub` | Docker registry credentials, type `kubernetes.io/dockerconfigjson` |
| `maprando-seeds-backblaze` | `application-key`, authorized for the seed bucket |
| `clouddns-dns01-solver-svc-acct` | `key.json`, Google Cloud DNS service account JSON |

Use private files under `secrets/`; do not put credential values in manifests or
shell history. For example, after supplying those files:

```bash
kubectl -n default create secret generic dockerhub \
  --type=kubernetes.io/dockerconfigjson \
  --from-file=.dockerconfigjson=secrets/docker-config.json
kubectl -n default create secret generic maprando-seeds-backblaze \
  --from-file=application-key=secrets/backblaze-application-key
kubectl -n default create secret generic clouddns-dns01-solver-svc-acct \
  --from-file=key.json=secrets/clouddns-key.json
kubectl apply -n default -f map-gen/map-gen.yaml
kubectl rollout status deployment/map-gen --timeout=900s
kubectl port-forward service/map-gen 5000:5000
```

In another terminal, check `curl --fail http://127.0.0.1:5000/health`, then test a
representative generation request using the request format supported by the
pinned image. The July image, embedded model filename, and legacy serving JSON
remain paired deliberately. This is not a deployment of the current map-gen
worktree. Current code uses `gpu_memory_fraction` instead of
`cuda_memory_fraction` and rejects several old fields. Update the image,
checkpoint, request schema, and serving JSON together when upgrading.

The serving pod uses eight worker threads, requests two CPUs, and retains the
old 6 GB RAM limit. Measure actual CPU/RAM/GPU usage under load before cutover.
Its startup probe allows 15 minutes for model initialization. Rolling updates
use `maxSurge: 1` and `maxUnavailable: 0` so the existing pod remains available
until the replacement is ready. Budget shared GPU slots and CPU/RAM for up to
two live versions and a replacement pod for each during simultaneous rollouts.
Verify readiness and in-flight request handling during a rollout before relying
on uninterrupted service.

```bash
kubectl apply -n default -f map-rando/maprando-dev.yaml
kubectl rollout status deployment/maprando-web-dev --timeout=600s
kubectl port-forward service/maprando-web-dev 8080:80
```

Open `http://127.0.0.1:8080` and generate a seed. This checks communication with
map-gen and access to the Backblaze seed repository. Map Rando still uses the
copied image pin; validate its compatibility before upgrading either application.

## Enable certificates and ingress

Review the Cloud DNS project and zone in `infra/issuer.yaml`. The namespaced
issuers use the Google service account secret in `default`. DNS-01 supports
certificate issuance without inbound web access. Start with the staging issuer:

```bash
kubectl apply -f infra/issuer.yaml
kubectl wait --for=condition=Ready issuer/letsencrypt-staging --timeout=300s
kubectl create --dry-run=client -f infra/certificate.yaml -o yaml \
  | sed 's/name: letsencrypt-prod/name: letsencrypt-staging/' \
  | kubectl apply -f -
kubectl wait --for=condition=Ready certificate/maprando-com --timeout=600s
kubectl apply -f map-rando/upload-limit.yaml -f map-rando/ingress.yaml
```

Forward the internal Traefik HTTPS service to the workstation:

```bash
kubectl -n kube-system port-forward service/traefik 8443:443
```

In another terminal, test routing with the intended hostname:

```bash
curl -k --connect-to experimental.maprando.com:443:127.0.0.1:8443 \
  https://experimental.maprando.com/
```

Staging certificates are not publicly trusted. Before production, apply the
production certificate and wait for the resulting certificate request and
certificate to become ready:

```bash
kubectl apply -f infra/certificate.yaml
kubectl get certificaterequests
kubectl describe certificate maprando-com
```

Confirm that the newly issued certificate uses the production issuer; the old
staging certificate can remain present while replacement is in progress. Then
repeat the HTTPS test without `-k`.

The ingress keeps `experimental.maprando.com`, uses Traefik, redirects HTTP to
HTTPS, and limits uploads to 8 MiB. After private application and HTTPS checks
pass, separately review and set up an OVHcloud VM running the frp server and an
outbound frp client in the cluster forwarding web traffic to Traefik. Keep the
Google Cloud reverse SSH proxy separate and Google Cloud DNS authoritative.

Review tunnel authentication, TLS handling, relay firewall rules, and DNS A/AAAA
records before switching public traffic. Do not forward home router web ports,
the Kubernetes API, or the Flannel VXLAN port to the internet.

## CI access

CI credentials and RBAC are applied separately from the application. If CI
needs to patch the experimental deployment, create the restricted service account
and generate a fresh kubeconfig explicitly from the rtx context:

```bash
kubectl config use-context rtx
kubectl apply -f map-rando/ci-image-updater.yaml
(cd map-rando && bash generate_ci_kubeconfig.sh)
```

The script reads the long-lived `image-updater-token` Secret from the current
context and writes `ci-kubeconfig.yaml` in the working directory. Run it after
the service-account token controller has populated the Secret.
A hosted runner also needs a VPN/tunnel to this private
API, or use a runner on the LAN. Do not expose port 6443 merely to make hosted
CI work. The service account retains the copied get/patch/list/watch permissions
restricted to the named `maprando-web-dev` deployment; list/watch requests must
select that resource by name. It cannot update map-gen. To rotate the token,
delete and reapply its Secret, then regenerate the kubeconfig and replace the
CI credential.

## Backups and rollback

Before upgrades, take an off-machine backup of `/etc/rancher/k3s`, the SQLite
directory `/var/lib/rancher/k3s/server/db`, and
`/var/lib/rancher/k3s/server/token`. Stop k3s while copying the SQLite directory
for a consistent backup; restart it promptly afterward. Protect backups as
credentials. Back up application persistent volumes separately if any are added.
The current application manifests have no persistent volumes.

Keep the old deployment available until seed generation and external HTTPS pass.
Rollback during cutover means restoring the old DNS/proxy route. Kubernetes
deployment rollback alone cannot undo DNS changes or guarantee compatibility
between an image, serving config, and checkpoint.

## References

- [K3s configuration](https://docs.k3s.io/installation/configuration)
- [K3s v1.36 releases](https://docs.k3s.io/release-notes/v1.36.X)
- [K3s Helm controller](https://docs.k3s.io/add-ons/helm)
- [K3s networking](https://docs.k3s.io/networking/networking-services)
- [NVIDIA Container Toolkit installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
- [NVIDIA device plugin](https://github.com/NVIDIA/k8s-device-plugin)
- [cert-manager supported versions](https://cert-manager.io/docs/releases/)
- [Kubernetes swap behavior](https://kubernetes.io/docs/concepts/cluster-administration/swap-memory-management/)
- [K3s backups](https://docs.k3s.io/datastore/backup-restore)

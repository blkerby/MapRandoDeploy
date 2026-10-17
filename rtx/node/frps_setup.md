# Set up frps

Run these commands on the proxy server.

## Install frps

```bash
curl -fLO https://github.com/fatedier/frp/releases/download/v0.71.1/frp_0.71.1_linux_amd64.tar.gz
tar -xzf frp_0.71.1_linux_amd64.tar.gz
sudo install -m 0755 frp_0.71.1_linux_amd64/frps /usr/local/bin/frps
sudo mkdir -p /etc/frp
```

## Configure authentication and TLS

Enter a root shell. The server configuration and systemd commands below run in
this shell; the Kubernetes section switches back to your laptop:

```bash
sudo -i
chmod 700 /etc/frp
umask 077
```

Generate the shared client authentication token. If `/etc/frp/token` already
exists from the initial setup, keep it and skip this command:

```bash
openssl rand -hex 32 > /etc/frp/token
```

Create an Ed25519 private key and self-signed certificate valid for approximately
1,000 years:

```bash
openssl req -x509 -newkey ed25519 -noenc \
  -keyout /etc/frp/server.key \
  -out /etc/frp/server.crt \
  -days 365000 \
  -subj "/CN=frp-relay" \
  -addext "subjectAltName=DNS:frp-relay"
```

If the certificate and key were already created, skip this command too. Running
it again replaces them and requires updating the certificate trusted by frpc.

The token authenticates frpc to frps. The certificate lets frpc verify the server;
the client will explicitly trust `server.crt` and use the TLS server name
`frp-relay`. This name does not require a DNS record. This certificate protects
the tunnel, separately from the website certificates managed by Traefik.
Ed25519 is not post-quantum authentication; any hybrid post-quantum TLS key
exchange is negotiated separately by the client and server.

Keep `token` and `server.key` private and outside the public repository. Later,
copy the token into a Kubernetes Secret and supply the public `server.crt` to
frpc. The private `server.key` stays on OVHcloud.

Write the server configuration:

```bash
cat > /etc/frp/frps.toml <<'EOF'
bindAddr = "0.0.0.0"
bindPort = 7000
proxyBindAddr = "0.0.0.0"

allowPorts = [
  { single = 80 },
  { single = 443 }
]

auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "/etc/frp/token"

transport.tls.force = true
transport.tls.certFile = "/etc/frp/server.crt"
transport.tls.keyFile = "/etc/frp/server.key"
EOF
```

frps accepts authenticated TLS tunnel connections on TCP port 7000. Clients may
register forwarding listeners only on ports 80 and 443. No administration
dashboard is enabled. PROXY protocol is configured later on frpc and Traefik.

## Run as a systemd service

Create an unprivileged service account and grant it read access to the files:

```bash
adduser --system --group --no-create-home frp
chown root:frp /etc/frp /etc/frp/frps.toml /etc/frp/token /etc/frp/server.key /etc/frp/server.crt
chmod 750 /etc/frp
chmod 640 /etc/frp/frps.toml /etc/frp/token /etc/frp/server.key /etc/frp/server.crt
```

Create the service:

```bash
cat > /etc/systemd/system/frps.service <<'EOF'
[Unit]
Description=frp tunnel server
After=network.target

[Service]
User=frp
Group=frp
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=on-failure
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
```

`CAP_NET_BIND_SERVICE` allows the service account to bind ports 80 and 443.

Start the service and enable it at boot:

```bash
systemctl daemon-reload
systemctl enable --now frps.service
systemctl status frps.service --no-pager
```

Expect `active (running)`. If startup fails, read the error:

```bash
journalctl -u frps.service -n 30 --no-pager
```

At this point, port 7000 listens for frpc. Ports 80 and 443 start listening when
frpc connects and registers its TCP proxies.

## Allow connections to the proxy server

Allow inbound TCP 7000, 80, and 443 through any active VPS/OVHcloud firewall,
preserving SSH access. Port 7000 receives the authenticated tunnel; 80 and 443
receive public web traffic. No home-router port forwarding is required.

## Prepare the Kubernetes credentials

On OVHcloud, stage copies readable only by the `ubuntu` SSH user:

```bash
install -o ubuntu -g ubuntu -m 0600 /etc/frp/token /home/ubuntu/frp-token
install -o ubuntu -g ubuntu -m 0600 /etc/frp/server.crt /home/ubuntu/frp-server.crt
```

Now switch to your laptop. Run the remaining commands from
`~/MapRandoDeploy/rtx`. Set the SSH alias and the public IPv4 address of the VPS:

```bash
cd ~/MapRandoDeploy/rtx
FRP_SSH_HOST=ovh
FRP_SERVER_IP=YOUR_OVH_PUBLIC_IPV4
umask 077
mkdir -p secrets
scp "$FRP_SSH_HOST:frp-token" secrets/frp-token
scp "$FRP_SSH_HOST:frp-server.crt" secrets/frp-server.crt
ssh "$FRP_SSH_HOST" 'rm -- frp-token frp-server.crt'
```

Replace `ovh` with your laptop's SSH alias (or `ubuntu@VPS_IP`) and replace
`YOUR_OVH_PUBLIC_IPV4` with the real address. The `secrets/` directory is ignored
by Git. Do not copy `server.key` off the VPS.

Create the Secret in the rtx cluster, using the local files directly:

```bash
kubectl --context rtx -n default create secret generic frp-credentials \
  --from-file=token=secrets/frp-token \
  --from-file=server.crt=secrets/frp-server.crt \
  --dry-run=client -o yaml | kubectl --context rtx apply -f -
```

The client needs no Kubernetes API permissions; the Secret is mounted as files.

## Configure Traefik for PROXY protocol

In `infra/traefik.yaml`, extend the existing `ports` section inside
`spec.valuesContent` to match this, preserving the other values:

```yaml
    ports:
      web:
        proxyProtocol:
          insecure: true
        http:
          redirections:
            entryPoint:
              to: websecure
              scheme: https
              permanent: true
      websecure:
        proxyProtocol:
          insecure: true
```

This is the agreed trust model: Traefik accepts PROXY metadata from any source
that can reach these internal entrypoints. It also continues accepting ordinary
connections without PROXY headers, including laptop port-forwards. Keep its
Service as `ClusterIP`, with ServiceLB disabled.

Apply the updated configuration:

```bash
kubectl --context rtx apply -f infra/traefik.yaml
```

The k3s Helm controller updates Traefik asynchronously. Before testing the tunnel,
wait for that update to finish. The resulting Traefik container arguments should
include `--entryPoints.web.proxyProtocol.insecure=true` and the corresponding
`websecure` argument. Configure this before deploying frpc, which will send a
PROXY header before the HTTP bytes or TLS handshake.

## Deploy frpc

Create `infra/frpc.yaml` with the following command on your laptop. The shell
inserts the `FRP_SERVER_IP` set above into the ConfigMap:

```bash
cat > infra/frpc.yaml <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: frpc-config
  namespace: default
data:
  frpc.toml: |
    serverAddr = "$FRP_SERVER_IP"
    serverPort = 7000

    auth.method = "token"
    auth.tokenSource.type = "file"
    auth.tokenSource.file.path = "/etc/frp/credentials/token"

    transport.tls.enable = true
    transport.tls.trustedCaFile = "/etc/frp/credentials/server.crt"
    transport.tls.serverName = "frp-relay"

    [[proxies]]
    name = "rtx-http"
    type = "tcp"
    localIP = "traefik.kube-system.svc.cluster.local"
    localPort = 80
    remotePort = 80
    transport.proxyProtocolVersion = "v2"

    [[proxies]]
    name = "rtx-https"
    type = "tcp"
    localIP = "traefik.kube-system.svc.cluster.local"
    localPort = 443
    remotePort = 443
    transport.proxyProtocolVersion = "v2"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: frpc
  namespace: default
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: frpc
  template:
    metadata:
      labels:
        app: frpc
    spec:
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
      containers:
      - name: frpc
        image: docker.io/fatedier/frpc:v0.71.1
        args: ["-c", "/etc/frp/config/frpc.toml"]
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        volumeMounts:
        - name: config
          mountPath: /etc/frp/config
          readOnly: true
        - name: credentials
          mountPath: /etc/frp/credentials
          readOnly: true
      volumes:
      - name: config
        configMap:
          name: frpc-config
      - name: credentials
        secret:
          secretName: frp-credentials
EOF

kubectl --context rtx apply -f infra/frpc.yaml
kubectl --context rtx -n default logs deployment/frpc
```

Expect successful server login and successful registration of both proxies.
If logs show the previous container during startup, repeat the logs command once
the new container is running. On OVHcloud, frps logs are available through
`journalctl -u frps.service`.

One client handles every hostname through Traefik. The `Recreate` strategy avoids
two clients trying to register the same proxy names and ports during an update.
Updating or restarting frpc briefly interrupts public access and can terminate
in-flight transfers. Application rollouts do not require restarting frpc.
No Kubernetes Service, host networking, or inbound rtx firewall rule is needed
for frpc. Its connection goes out to the VPS's public IPv4 address, which the
rtx egress firewall allows.

## Test before changing DNS

The application's Deployment, Service, Ingress, middleware, and production TLS
certificate must already be installed. For the existing experimental hostname,
test from your laptop:

```bash
curl -I --resolve "experimental.maprando.com:80:$FRP_SERVER_IP" \
  http://experimental.maprando.com/
curl --resolve "experimental.maprando.com:443:$FRP_SERVER_IP" \
  https://experimental.maprando.com/
```

Expect an HTTP-to-HTTPS redirect from the first command and the application page
with a trusted certificate from the second. `--resolve` changes the destination
only for this request, preserving the HTTP hostname and TLS SNI without changing
public DNS. A Traefik 404 indicates the tunnel reached Traefik but the hostname
or Ingress routing did not match. A default/untrusted certificate indicates TLS
certificate configuration still needs attention; do not bypass it for the final
production test.

Once these tests pass and you are ready to move traffic, update the intended
Google Cloud DNS A record to the VPS public IPv4 address. Remove or update any
AAAA record that would otherwise send IPv6 clients to the old server; this
configuration publishes IPv4 listeners only. Retain the old destination for
rollback. Future services need DNS records pointing to this VPS and their own
Traefik Ingress resources; the frp configuration remains the same.

## Client updates and rollback

After changing the ConfigMap or rotating the token/certificate, restart frpc to
load the new files:

```bash
kubectl --context rtx apply -f infra/frpc.yaml
kubectl --context rtx -n default rollout restart deployment/frpc
```

For token rotation, update `/etc/frp/token` on OVHcloud and the Kubernetes Secret,
then restart frps and frpc. For certificate replacement, update the public
certificate in the Secret as well as the server key/certificate on OVHcloud,
then restart both processes.

To stop public forwarding without touching application pods:

```bash
kubectl --context rtx -n default scale deployment/frpc --replicas=0
```

Scaling back to one reconnects the tunnel. If public DNS has been changed,
restoring service through the old host also requires reverting its DNS record.

## References

- [frp v0.71.1](https://github.com/fatedier/frp/releases/tag/v0.71.1)
- [Server configuration](https://gofrp.org/en/docs/reference/server-configures/)
- [Token authentication](https://gofrp.org/en/docs/features/common/authentication/)
- [TLS configuration](https://gofrp.org/en/docs/features/common/network/network-tls/)
- [Client configuration](https://gofrp.org/en/docs/reference/client-configures/)
- [PROXY protocol](https://gofrp.org/en/docs/features/common/realip/)
- [Official frpc image](https://hub.docker.com/r/fatedier/frpc/tags)
- [Traefik entrypoints](https://doc.traefik.io/traefik/reference/install-configuration/entrypoints/)

# Outbound LAN restrictions

`egress.nft` restricts traffic leaving rtx through `enp3s0`, covering both host
processes and forwarded pod/Docker traffic. It blocks private and link-local IPv4
destinations, IPv4 multicast/broadcast, and outbound IPv6 application connections.
IPv4 internet access is allowed. IPv6 neighbor discovery is allowed; rtx currently
has no IPv6 internet route.

Exceptions allow host DHCP and TCP/UDP DNS to `192.168.0.1`. Replies to inbound
connections and existing tracked connections are allowed. Existing outbound LAN
connections therefore remain usable until their connection-tracking entries
expire. This is not an inbound firewall; existing inbound rules still apply.

Only traffic leaving the physical Ethernet interface is restricted, preserving
loopback and internal Kubernetes/Docker networking. The separate `rtx_egress`
table does not replace Kubernetes or Docker tables. Connections to other private
networks through Ethernet are blocked too. Additional physical interfaces or VPNs
would require reviewing this policy. Root on rtx can remove these restrictions.

## Temporary application

These commands are for a separately approved test on rtx, from the copied `rtx`
directory. Bootstrap does not install or apply this file.

```bash
sudo nft -c -f node/egress.nft
sudo nft -f node/egress.nft
```

The first command asks the kernel to validate the rules without applying them.
The second applies them atomically. Repeating it replaces only this table.
Do not run `nft flush ruleset`: that would also remove Kubernetes/Docker rules.

Keep a laptop SSH session open. Verify a fresh laptop SSH connection and kubectl
access, host and pod internet/DNS access, and blocked host/pod connections to a
known reachable home service. Inspect blocked-packet counters with:

```bash
sudo nft list table inet rtx_egress
```

Remove these restrictions with:

```bash
sudo nft delete table inet rtx_egress
```

## Load at boot

After the temporary test succeeds, install the dedicated systemd service. Run
on rtx from the copied `rtx` directory:

```bash
sudo install -m 0644 node/egress.nft /etc/rtx-egress.nft
sudo install -m 0644 node/rtx-egress.service /etc/systemd/system/rtx-egress.service
sudo systemctl daemon-reload
sudo systemctl enable --now rtx-egress.service
```

The service loads only this table before network configuration starts. It does
not enable the distribution's `nftables.service`, change `/etc/nftables.conf`,
or save the live Kubernetes/Docker rules. A load failure marks the service failed;
it does not prevent networking from starting.

After changing the rules, install the updated file and reload atomically:

```bash
sudo install -m 0644 node/egress.nft /etc/rtx-egress.nft
sudo systemctl reload rtx-egress.service
```

To disable persistence and remove the restrictions:

```bash
sudo systemctl disable --now rtx-egress.service
```

Stopping the service deletes only `inet rtx_egress`. Once it manages the table,
use the service commands rather than deleting the table manually. Do not save
the complete live Kubernetes/Docker ruleset as a static boot configuration.

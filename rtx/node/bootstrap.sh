#!/usr/bin/env bash
set -euo pipefail

node_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=versions.env
source "$node_dir/versions.env"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg
bootstrap_tmp=$(mktemp -d)
trap 'rm -rf -- "$bootstrap_tmp"' EXIT
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  -o "$bootstrap_tmp/nvidia.asc"
gpg --batch --yes --dearmor \
  --output /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
  "$bootstrap_tmp/nvidia.asc"
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  -o "$bootstrap_tmp/nvidia.list"
sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  "$bootstrap_tmp/nvidia.list" > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y --no-install-recommends \
  "nvidia-container-toolkit=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
  "nvidia-container-toolkit-base=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
  "libnvidia-container-tools=$NVIDIA_CONTAINER_TOOLKIT_VERSION" \
  "libnvidia-container1=$NVIDIA_CONTAINER_TOOLKIT_VERSION"

install -d -m 0755 /etc/rancher/k3s
install -m 0600 "$node_dir/k3s-config.yaml" /etc/rancher/k3s/config.yaml

# The upstream installer verifies the downloaded k3s binary's checksum.
curl -fsSL "https://raw.githubusercontent.com/k3s-io/k3s/$K3S_VERSION/install.sh" \
  -o "$bootstrap_tmp/install-k3s.sh"
INSTALL_K3S_VERSION="$K3S_VERSION" sh "$bootstrap_tmp/install-k3s.sh" server

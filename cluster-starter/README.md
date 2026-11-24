# CM5 / K3s cluster starter

This independent, deliberately generic Ansible starter prepares **one Debian-family
master and one worker** (including Raspberry Pi OS / CM5 when compatible).
It does not deploy anything until you configure the inventory and explicitly
enable components. No private infrastructure is embedded. Review every variable,
artifact and task before use; test on disposable machines first.

Physical OS provisioning, network/VPN/firewall configuration, credentials,
SSH trust, and hardware-specific setup remain the operator's responsibility.

## Architecture

Both nodes can get base tools, an unprivileged SSH service account, distribution
Docker, a CIFS NAS mount, Netdata, and Prometheus node exporter. The master can
have a whole-disk Btrfs mount, K3s server, host PostgreSQL and Prometheus. The
worker can join as a K3s agent and install a vendor Hailo DKMS host driver.
Optional Kubernetes node labels describe roles, enabled storage and acceleration;
they are **not** a storage provisioner, scheduler policy, Hailo device plugin,
container runtime integration, database backup, or high-availability setup.
Docker is separate from the containerd runtime used by K3s.

## Start from a pre-release ZIP

From the repository's GitHub Releases page, download the ZIP and matching
`.sha256` asset from a **Cluster starter** pre-release. Verify the archive
before extracting it:

```sh
sha256sum -c cluster-starter-vX.Y.Z.zip.sha256
unzip cluster-starter-vX.Y.Z.zip
cd cluster-starter-vX.Y.Z
```

Replace `vX.Y.Z` with the exact version in the downloaded asset names. Then
follow the controller setup, inventory, and secrets steps below. The package
does not contain credentials, a production inventory, SSH keys, or the Hailo
vendor driver.

Maintainers can create a pre-release from the GitHub Actions page by running
**Cluster starter pre-release** on the intended repository revision and
entering a new `vMAJOR.MINOR.PATCH` version (optionally with a prerelease
suffix). The workflow validates the playbook and safety checks before
publishing.

## Controller setup

From `cluster-starter/` on a trusted controller with Python 3 and SSH:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-controller.txt
ansible-galaxy collection install -r requirements.yml
```

Use Debian 12-compatible repositories, systemd, Python 3, sudo/become access,
and network access on the managed nodes. Raspberry Pi kernel header package
names can differ: the Hailo role intentionally fails if
`linux-headers-<running kernel>` is unavailable; install appropriate headers or
adapt the role to your OS. Hailo drivers must match the hardware, kernel and
architecture. The operator supplies a trusted vendor `.deb` and checksum.
The host driver alone does not grant pods access to the accelerator.

## Inventory and secrets

Copy `inventory/example` to a private inventory directory under `inventory/`.
Replace **every value used by an enabled role** marked `REPLACE_WITH`, including
SSH user and addresses, DNS-compatible K3s node names, and network addresses.
The controller must know the hosts' SSH keys; host key checking stays enabled.
Avoid keeping a real inventory in version control.

Create `group_vars/all/` in your private inventory. Copy
`inventory/vault.example.yml` to `group_vars/all/vault.yml` there,
replace the needed values and encrypt it:

```sh
mkdir -p inventory/YOUR_INVENTORY/group_vars/all
cp inventory/vault.example.yml inventory/YOUR_INVENTORY/group_vars/all/vault.yml
ansible-vault encrypt inventory/YOUR_INVENTORY/group_vars/all/vault.yml
ansible-vault edit inventory/YOUR_INVENTORY/group_vars/all/vault.yml
```

Use a unique, random K3s token and PostgreSQL password. Vault protects data at
rest on the controller; NAS credentials and the K3s configuration are written
root-only on nodes. The optional Kubernetes Secret is also stored in the
cluster's datastore and in a root-only manifest on the master; Kubernetes
Secrets are not automatically encrypted merely because they are base64 encoded.
Set up encryption at rest, RBAC, network policy and key management yourself.
Do not pass secrets on a command line or commit an unencrypted vault file.

All `enable_*` switches default to `false`. Enable only intended components in
your inventory. The playbook always checks exactly one distinct master and
worker before connecting. Typical initial order is common/account, then
storage/NAS, then K3s, then host services and labels. The single site play
follows that order; selectively change switches and run it again as required.
`enable_postgresql_k8s` requires both `enable_postgresql` and `enable_k3s`.
`enable_cluster_metadata` requires `enable_k3s`. Avoid changing a live cluster's
K3s token or node names without a separate migration plan.

## Storage safety

Storage is disabled by default and only applies to the master. Set
`storage_device` to a stable `/dev/disk/by-id/...` path for a **whole disk**,
`storage_expected_serial` to its actual `lsblk` serial, and a dedicated mount
directory. `storage_mode: existing` only mounts an existing Btrfs filesystem;
it never formats. Verify the device, serial, backups and mount target manually.

To format a **blank** disk, set `storage_mode: format`, explicitly set
`storage_format_approved: true`, and set `storage_format_confirm_serial` to
the exact same serial as `storage_expected_serial`. The guard verifies the
actual disk serial, whole-disk type, no partitions/dependent devices, no
mount/swap, and no existing filesystem or `wipefs` signatures; it refuses to
erase anything already in use. Existing Btrfs is reused, not reformatted.
This is not a secure erase; device changes between inspection and formatting
remain a race, so never reconfigure storage concurrently. Don't use check mode
as approval and don't put other important data under the mount point. Formatting
is irreversible. K3s's default local-path provisioner is not automatically
redirected to this mount; configure a separate StorageClass yourself.

## K3s and network

Specify an exact version such as `vX.Y.Z+k3sN`, the server address reachable by
both nodes, a Vault token, and the SHA-256 of the HTTPS installer script you
have independently verified. K3s's installer downloads the selected release;
the release is pinned but the script checksum alone does not authenticate the
downloaded release independently. Permit TCP 6443 from worker/controller to
master and allow required K3s CNI traffic between nodes. Review the K3s
documentation for firewall ports and kernel prerequisites. Version changes are
rejected unless `k3s_allow_version_change: true`; plan upgrades/backups first.
The master API wait tests TCP connectivity, while node labeling retries until
the worker is registered. See `k3s kubectl get nodes` for readiness.

Netdata and Prometheus HTTP listeners default to loopback. Node exporter
normally listens on all interfaces on TCP 9100; firewall it to the master.
This package replaces Netdata's config and Prometheus's scrape config when
enabled. Netdata is reachable over SSH tunneling unless configured otherwise.
NAS uses CIFS, a root-only credentials file, `_netdev,nofail`, and does not
grant the service account special NAS permissions by itself.

PostgreSQL defaults to loopback and uses peer authentication for local
administration. For remote/Kubernetes access set `postgresql_allow_remote`,
an explicit master IP as `postgresql_listen_address`, and a narrowly scoped
`postgresql_client_cidr` appropriate to the actual pod source addresses; check
firewall/NAT and existing `pg_hba.conf` rules. Enabling Kubernetes access
creates a selectorless Service, static Endpoints and a credentials Secret in the
specified namespace. It does not expose PostgreSQL outside the cluster or
enable TLS. Secure the database transport separately. PostgreSQL lives on the
OS default data directory; the Btrfs mount does not move it automatically.
Disable or remove previously created Service/Endpoints/Secret manually when
turning the feature off; disabling roles does not uninstall resources.

## Validate and operate

With the controller dependencies installed, from this directory:

```sh
python3 -m unittest discover -s tests -v
ansible-inventory -i inventory/example/hosts.yml --graph
ansible-playbook -i inventory/example/hosts.yml playbooks/site.yml --syntax-check
ansible-lint playbooks/site.yml
# After replacing the placeholders and encrypting the vault:
ansible-playbook -i inventory/YOUR_INVENTORY/hosts.yml playbooks/site.yml --ask-vault-pass --ask-become-pass --check --diff
ansible-playbook -i inventory/YOUR_INVENTORY/hosts.yml playbooks/site.yml --ask-vault-pass --ask-become-pass
```

Syntax checking and the local unit tests do not contact nodes. Check mode cannot
fully predict first installs, service readiness, filesystem formatting or
Kubernetes mutations; inspect failures and plan before an actual run. The last
command **changes remote machines** and is an operator action, not part of this
starter's validation. Provision backups and a recovery plan first.

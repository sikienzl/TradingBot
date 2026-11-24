# Ansible starter for the paper dashboard

This is a small, generic deployment starter for the read-only Paper Trading
Dashboard. It provisions the dashboard service account and Docker on an
already-installed Debian/Ubuntu host, builds the dashboard image from the
TradingBot repository, and installs it as a systemd system service.

It is **not** a complete cluster bootstrap. It does not configure K3s, Hailo
drivers, storage/NAS, VPN routing, host firewall rules, the paper-trading bot,
or an exchange. Those are environment-specific and should be reviewed and
managed separately. No production inventory, host address, password, CIDR,
certificate, or private key belongs in this directory.

## Requirements

- Ansible on the controller; no extra Ansible collections are required.
- An existing Debian/Ubuntu target reachable over SSH.
- A remote SSH account with `sudo` access.
- A network route and firewall policy that allow dashboard TCP access only
  from intended private LAN/VPN client networks.
- Existing paper-bot data and report directories on the target.
- A TLS certificate and matching private key whose Subject Alternative Name
  covers the address/name clients use.
- A long, unique dashboard password and one or more private CIDRs.

The role creates the `trading` system user/group by default and adds it to the
Docker group. Docker group membership grants root-equivalent access to the
host; this must be an explicit operator decision. The dashboard process runs
as that unprivileged account, drops container capabilities, uses a read-only
container root, and mounts paper data, reports, and TLS files read-only.

## Prepare private inventory and variables

Run commands from this directory. Copy the examples to the paths Ansible reads;
the real inventory and encrypted vault are ignored by the parent repository:

```sh
cp inventory/hosts.yml.example inventory/hosts.yml
cp group_vars/paper_dashboard/vars.yml.example \
  group_vars/paper_dashboard/vars.yml
cp group_vars/paper_dashboard/vault.yml.example \
  group_vars/paper_dashboard/vault.yml
mkdir -p files
```

Edit `inventory/hosts.yml` with the actual DNS name/address and SSH user.
Verify the target SSH host-key fingerprint through a trusted channel before
connecting. Do not disable SSH host-key checking or accept an unverified key.

Edit `group_vars/paper_dashboard/vars.yml` to match the target's paper data
and report directories, source revision, and local TLS file paths. Replace
all `REPLACE_WITH_...` values. Use a reviewed release tag or immutable commit
SHA for the source revision. The role deliberately fails while these example
values remain in place.

Put the target bind address, **all** authorized private IPv4/IPv6 CIDRs, and a
unique long password in `group_vars/paper_dashboard/vault.yml`. The bind
address must belong to the target host.
The dashboard checks the actual client source address against this allowlist
and rejects public networks. It does not trust forwarded-IP headers, so a VPN
route must preserve the client source address.

Encrypt the vault file before running Ansible:

```sh
ansible-vault encrypt group_vars/paper_dashboard/vault.yml
```

Create `files/dashboard.crt` and `files/dashboard.key` on the controller, or
set the source paths in `vars.yml` to securely provisioned local files. Ensure
the certificate SAN matches the address/name used in browsers. Keep the key
private (`0600`) and never commit either file; the parent `.gitignore` excludes
certificate, key, and PEM files in `ansible/files/`.

## Validate and deploy

```sh
ansible-inventory --list
ansible-playbook -i inventory/hosts.yml \
  playbooks/dashboard.yml --syntax-check
ansible-playbook -i inventory/hosts.yml playbooks/dashboard.yml \
  --ask-vault-pass --ask-become-pass --check
ansible-playbook -i inventory/hosts.yml playbooks/dashboard.yml \
  --ask-vault-pass --ask-become-pass
```

Use the check-mode preview to review paths, package installation, and user/group
changes before applying. Some Docker build and service actions only run outside
check mode. Never print or diff the decrypted vault or private key.

The role installs a system-level systemd unit. This avoids depending on a
logged-in user manager or user-session lingering. It grants the unit's service
account the Docker supplementary group explicitly; the container itself has
no Docker socket mount. The network host mode preserves the real remote client
IP for the dashboard's CIDR check. Keep the host/network firewall restrictive
as a separate layer of defense.

## Verify and operate

```sh
systemctl status trading-paper-dashboard.service
journalctl -u trading-paper-dashboard.service -n 50 --no-pager
docker ps --filter name=trading-paper-dashboard
ss -ltn | grep ':8443'
```

Test that an unauthenticated HTTPS request receives `401`, and that a valid
login succeeds only from a listed source CIDR. Configure the browser to trust
the certificate after verifying its fingerprint over a trusted channel. To
update the dashboard, change `public_paper_dashboard_git_version` to a reviewed revision
and rerun the playbook. This role does not restart or modify the paper-bot
container.

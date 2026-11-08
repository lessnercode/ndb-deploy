# How to deploy NDB on AHV

Use the `ndb-deploy` skill to deploy the NDB appliance, register its cluster, and prepare database profiles. The reusable commands handle the API work and save progress between stages.

This guide stops at profiles. Creating database instances or source VMs is a separate task.

## Example deployment

The examples use documentation-only addresses. Replace them with values for your environment; they are not a live deployment.

| Component | Example |
| --- | --- |
| Prism Central | `https://192.0.2.10:9440` |
| Prism Element | `192.0.2.37` |
| AHV cluster | `AHV-EXAMPLE` |
| Database VLAN | `database-vlan` |
| NDB appliance | `https://192.0.2.140` |
| Appliance subnet | `192.0.2.128/25` |
| Gateway | `192.0.2.129` |
| DNS | `192.0.2.6` |
| Installer-serving machine | `192.0.2.240` |

To check an existing deployment, ask the agent:

```text
Use $ndb-deploy to check the installation using my deployment config.
Report setup and profile status. Stop at profiles.
```

Or run:

```bash
python3 "$HOME/.codex/skills/ndb-deploy/scripts/ndb.py" \
  --config "$HOME/ndb-deployment/config.json" status
```

Add `--details` to list every profile. Use a separate config and state file for each deployment.

## Before a new deployment

Have these items available:

- Prism Central URL and credentials.
- Target AHV cluster and VLAN subnet.
- A free static IP, subnet prefix, gateway, and DNS server for the NDB appliance.
- The NDB AHV QCOW2 installer and its published checksum.
- Prism Element address and an account with cluster-administrator access.
- The boot-disk storage container and the database storage container.
- VM sizing from the documentation for the selected NDB release.

Use your local wiki first. The paths below are relative to its root:

| Topic | Local wiki page |
| --- | --- |
| Installation and static IP | `wiki/ndb/ndb-installation-and-networking.md` |
| Appliance sizing | `wiki/ndb/ndb-control-plane-scalability.md` |
| Profile requirements | `wiki/ndb/ndb-profiles-provisioning-and-cloning.md` |
| SQL Server | `wiki/ndb/sql-server-management.md` |
| PostgreSQL and vector search | `wiki/ndb/postgresql-management.md` |

Confirm the selected release supports the target environment. The example settings are not a sizing or compatibility requirement for every deployment.

## 1. Create a deployment config

Python 3.11 or later is required. The following commands use a Bash-compatible shell on Linux or macOS. Adjust the skill location if it is installed elsewhere.

Create a working directory, copy the example config, and define a terminal shortcut:

```bash
NDB_DEPLOY_SKILL="$HOME/.codex/skills/ndb-deploy"
NDB_DEPLOY_WORK="$HOME/ndb-deployment"
NDB_DEPLOY_CONFIG="$NDB_DEPLOY_WORK/config.json"

mkdir -p "$NDB_DEPLOY_WORK"
cp "$NDB_DEPLOY_SKILL/references/config.example.json" "$NDB_DEPLOY_CONFIG"

ndb() {
  python3 "$NDB_DEPLOY_SKILL/scripts/ndb.py" \
    --config "$NDB_DEPLOY_CONFIG" "$@"
}
```

Edit `config.json`. Replace all example paths, addresses, IDs, and checksum values.

| Setting | What to enter |
| --- | --- |
| `credentials_file` | An absolute path to a protected credential file in the new working directory. |
| `state_file` | A unique absolute path for this deployment's progress. |
| `prism.url` | The Prism Central HTTPS URL, including port 9440. |
| `ndb.url` | The HTTPS URL of the planned static appliance IP. |
| `cluster_name`, `pe_address` | The target cluster name and its Prism Element VIP. |
| `deployment` | VM name, cluster/subnet IDs, static network settings, boot container, and sizing. |
| `database_container` | Storage for future database disks. This can differ from the appliance boot container. |
| `image` | Installer path, image name, published checksum, and a source URL the cluster can reach. |
| `subnet_name`, `profiles` | The database VLAN name and the supporting profiles to prepare. |

Keep passwords out of the config. TLS verification is enabled by default. Use a trusted CA where available; the example's self-signed-certificate option is intended for a confirmed lab appliance.

## 2. Enter credentials and discover resources

Save Prism Central credentials through the terminal prompts:

```bash
ndb credentials --account pc
ndb discover
```

Use the returned IDs to complete the cluster, subnet, and boot-container settings. The command also checks which API version Prism accepts.

Save the Prism Element credentials separately:

```bash
ndb credentials --account pe
```

The prompts store credentials with owner-only permissions. Prism Central and Prism Element can use different accounts or passwords.

## 3. Check the static network settings

```bash
ndb preflight
```

This checks that the subnet and boot container belong to the chosen cluster, the network settings agree with Prism, and the proposed address is outside the DHCP pool and absent from Prism's assignment/reservation records.

Also check IPAM and ARP on the target VLAN before using the address. No ping response alone does not prove an IP is free. If the agent has already been asked to select a free address, it can perform these checks and update the config.

Run preflight before creating the VM. After deployment, the appliance's own address should appear occupied.

## 4. Download and verify the installer

Sign in to the Nutanix Support Portal and download the AHV QCOW2 image for the selected release. Record the checksum shown beside that exact download and enter it in the config.

You can keep the package in your Downloads folder or another directory readable by the deployment process. For example:

```text
~/Downloads/NDB-Server-<release>.qcow2
```

Set the config's `image` section to match the actual file. Replace `<release>` and the checksum placeholder:

```json
{
  "image": {
    "name": "NDB-<release>-AHV",
    "file": "~/Downloads/NDB-Server-<release>.qcow2",
    "checksum_algorithm": "sha256",
    "checksum": "<published checksum for this file>",
    "source_url": "http://192.0.2.240:8765/NDB-Server-<release>.qcow2"
  }
}
```

Use the checksum algorithm actually published for the download. The CLI accepts `sha256`, `sha1`, and `md5`. The example source IP must be replaced with the installer-serving machine's cluster-reachable address. Its URL filename must match the local filename.

```bash
ndb verify-image
```

Continue only after the checksum matches. A previously downloaded installer can be reused if it passes verification.

## 5. Import the image

If the installer is local, set `image.source_url` to the local machine's cluster-reachable address and installer filename. Start the file server in a separate terminal, using the same config:

```bash
python3 "$HOME/.codex/skills/ndb-deploy/scripts/ndb.py" \
  --config "$HOME/ndb-deployment/config.json" serve-image
```

In the deployment terminal:

```bash
ndb import-image
```

The import command waits for Prism's task to complete. Stop the file server with `Ctrl+C` after a successful import. If the installer is already available at a cluster-reachable source URL, omit the local file server.

Confirm that `deployment.boot_container_id` matches the imported image's source container before creating the VM. For example, an image imported into `SelfServiceContainer` may require its cloned boot disk to stay there, while future database disks use a separate container such as `database-storage`. Confirm the source container in your environment.

## 6. Create and start the appliance

```bash
ndb create-vm
ndb power-on
```

The VM uses Legacy BIOS, an explicit disk-first boot order, and cloud-init to set the static address.

Allow a few minutes for first boot. Prism can show a temporary DHCP address before the static address is applied. Confirm cloud-init completes and HTTPS responds at the configured static IP. If cloud-init reports an error, use the local wiki's console procedure before making further network changes.

## 7. Initialize NDB and register the cluster

Within the authorized installation scope, initialize the administrator account and accept the installation terms:

```bash
ndb initialize
```

The command generates an administrator password, saves it in the protected credential file, and verifies authentication. If accepting the installation terms is outside the requested scope, stop before this command.

Register the cluster and start NDB setup:

```bash
ndb configure
ndb status
```

NDB creates its built-in software profiles during setup. This can involve temporary VMs and copying large disks. Progress can remain at one percentage for several minutes.

Check `status` periodically. Continue to network-profile creation after **Configure OOB software profiles** reports status **`5`**, meaning success, and **100%** completion.

Use the cluster's NTP servers for the appliance and verify clock synchronization. SMTP, telemetry, and high availability are separate configuration choices.

## 8. Prepare profiles and stop

```bash
ndb profiles
ndb status
```

The command creates or reuses the configured compute and network profiles. Existing database-parameter defaults can be reused. Confirm the requested profile versions show **`READY`**.

For SQL Server and PostgreSQL/vector work:

| Component | Requirement |
| --- | --- |
| Compute profile | CPU and memory settings for a future VM. Creating it does not allocate a VM. |
| Network profile | The selected database VLAN. The command supports DHCP-managed database VLANs. |
| Parameter profile | Use the SQL Server or PostgreSQL defaults where suitable. |
| SQL Server software profile | A registered source VM with SQL Server installed. |
| PostgreSQL/pgvector software profile | A suitable registered PostgreSQL source image with pgvector installed. A bundled PostgreSQL image does not establish that pgvector is included. |

A static IP on the NDB appliance does not change the database VLAN into a static, NDB-managed IP pool.

If no source VMs are available, stop after the supporting profiles and record the software-image dependency. Do not create source VMs or database instances to work around a profiles-only stopping point.

## Resume an interrupted deployment

Use the same config and state file. Rerun the interrupted stage to poll its saved task.

If the command reports an uncertain write or a `pending` marker, inspect the task and actual resource first. Do not clear the marker just to retry. Reconcile the saved task/resource IDs before continuing.

A conflicting resource name requires inspection. The commands do not automatically delete or replace existing resources.

## Change the administrator password

Using the terminal shortcut defined above:

```bash
ndb rotate-password
```

The command generates a new password, updates the protected credential file, and verifies it through a fresh session. If rotation is interrupted, preserve the protected recovery candidate and verify which password works before retrying.

## Completion check

Confirm these results before handing over the installation:

- The static NDB HTTPS address responds.
- The registered cluster is `UP`.
- Required setup reports success.
- Requested supporting profile versions are `READY`.
- The database count remains zero when stopping at profiles.
- Missing SQL Server or pgvector software images are recorded as pending source images.

Report the appliance URL, username, credential-file location, and any remaining source-image requirements. Keep passwords out of the guide and routine status output.

# Deployment and recovery

These implementation notes come from a successful NDB 2.11.1 deployment on AOS/PC 7.5.1. They are evidence for that environment, not a blanket compatibility promise. Use the matching local wiki/version documentation when deploying a different release.

## Inputs and order

Required: Prism Central URL and credentials, cluster/subnet IDs, free static IP/prefix/gateway/DNS, installer/checksum/source URL, image boot container, VM socket/core/RAM sizing, PE address/credentials, NDB URL, database storage container. Keep each deployment in a separate state file. JSON config must not contain passwords.

Use `discover → preflight → verify-image → serve-image + import-image → create-vm → power-on → initialize → configure → status → profiles → status`. Run only the stages needed for the user's request. `serve-image` remains active during import and must be stopped afterward. If an artifact is hosted elsewhere already, use its source URL and omit the local server.

NDB supports a console alternative if cloud-init cannot configure networking. The CLI does not automate SSH, console access, or selecting a free address. Use the wiki's `era-server` procedure if needed; confirm first-boot cloud-init is finished before replacing a working configuration.

## Observed requirements

- The installed MCP catalog used newer VMM/cluster API versions than PC accepted. The CLI probes VMM v4.3, v4.2, then v4.0 on read requests. Authentication failures are not version fallback signals. Cluster/subnet inventory uses v4.0; v3 VM creation is the proven fallback.
- Image import task success must precede cloning. The source image in this lab lived in `SelfServiceContainer`; cloning directly into the `ndb` container failed. Set `deployment.boot_container_id` to the image's actual source container, confirmed in PE/image metadata. This is separate from `database_container`, where future database disks will live. Do not infer the source container from a generic name on other clusters.
- Legacy BIOS must have explicit boot order. The v3 field is `boot_device_order_list`.
- Image disk size comes from Prism's virtual image size, not the compressed QCOW2 file size. The default firmware, virtual image size, and sizing must be checked against the selected NDB release's wiki guidance.
- Static IP cloud-init invokes `configure_static_ip ip=… gateway=… netmask=… nameserver=…`. The VM can show an initial DHCP address before final configuration completes. First boot took several minutes. Check cloud-init status and HTTPS before diagnosing a failure.
- Power-on needs an `If-Match` ETag plus `Ntnx-Request-Id`. Send no body.
- NDB login requires `/auth/validate?token=true&expire=20`; omitting expiry produced a misleading server error. Initial-login cookies must persist through `/auth/update?accept-eula=true`. Password changes additionally require `currentPassword`. A new connection verifies the changed password.
- PE registration uses the PE VIP and PE credentials, not PC credentials. Confirm the PE account works before registering. Registration uploads the connection JSON as a multipart `file` field, then storage selection triggers built-in software-profile setup.
- NDB setup creates temporary profile VMs and copies/converts 100 GB disks. Poll the setup operation; do not call it stuck solely because progress stays unchanged for a few minutes. Its success is status `5`; steps may retain skipped/unused entries. Network profile creation failed before final server metadata was initialized and succeeded after setup completed.
- An appliance may have a static OS IP on an AHV DHCP-managed VLAN. That does not make the VLAN NDB-managed/static. Do not change its type or create IP pools just to match the appliance address.

## State and error handling

State contains deployment identity, task/resource IDs, and a `pending` write marker, never passwords. Task IDs are checkpointed with clearing the marker in one atomic write. Polling timeouts retain the task so the same command resumes polling. Failed tasks stop the stage; read their child tasks for the actual cause.

A `pending` marker means a response may have been lost. Inspect the named operation and actual resources. Only after reconciling the outcome should an agent update the local state (resource/task ID and marker). Do not clear a marker merely to retry. Never delete existing VM/image/profile resources automatically. Name collisions must match the intended configuration before adoption. Config changes that affect deployment identity require a separate state file.

For a password failure, the `.pending` credential file is deliberately retained. Privately verify the old/candidate password, preserve the working value, and reconcile the state marker. Never expose either candidate or server response bodies in logs.

## TLS and credentials

TLS verification is enabled by default; use a CA file when available. `allow_self_signed` is an explicit per-service choice for a confirmed lab appliance. Never silently disable PC verification. The current PC hostname matches its certificate; the PE/NDB appliances used bundled self-signed certificates. API redirects are refused to avoid forwarding authorization to another origin.

Protected JSON credentials use keys `pc_username`, `pc_password`, `pe_username`, `pe_password`, `ndb_username`, `ndb_password`. Environment alternatives for PC/NDB are `PC_USERNAME`, `PC_PASSWORD`, `NDB_USERNAME`, `NDB_PASSWORD`. The optional `prism.credentials_env_file` reads existing MCP `PC_USERNAME`/`PC_PASSWORD` without copying them. Both files must have owner-only permissions.

## Boundaries

The CLI covers VLAN/AHV deployment and supporting compute/network profiles. VPC overlays, static DB IP pools, software-image capture, engine installation, CA replacement, HA, and database provisioning need the matching wiki workflow and requested scope. NTP can be set through `PUT /era/v0.9/config/era-server?config-params=ntp,` with `ntpServers`; use the cluster's configured servers, then verify clock synchronization. No SMTP or telemetry changes are required by this skill.

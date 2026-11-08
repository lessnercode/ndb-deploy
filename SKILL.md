---
name: ndb-deploy
description: Deploy and initialize Nutanix Database Service on AHV, resume deployment stages, inspect setup, prepare database profiles, or rotate its administrator password. Use the local NDB wiki and reusable API scripts; database instance provisioning is a separate scope.
---

# NDB deployment

Use the bundled CLI for repeatable API operations. Keep tool output to the stage result and actionable failures. Do not reread source code, entire manuals, browser JavaScript, or old transcripts during routine use.

## Start with scope and existing state

Identify the requested outcome: inspect, deploy appliance, initialize/register cluster, prepare profiles, or change password. An installation request authorizes its required stages; do not add a new approval gate. Preserve an explicit stopping point such as “profiles only.” Database instances and source/gold VMs require their own requested scope.

1. Look for an existing deployment config in the workspace. Check state and run `status` for an existing appliance before any deployment command.
2. Read only the relevant local wiki pages. Use `wiki_path` in the deployment config, or locate the available vault. For deployment use `ndb-installation-and-networking.md` and `ndb-control-plane-scalability.md`. For SQL Server or PostgreSQL/vector work use the engine page and `ndb-profiles-provisioning-and-cloning.md`. Read matching raw source sections only when the summary leaves an implementation question.
3. Use [references/config.example.json](references/config.example.json) for a new config. Replace placeholder IDs using `discover`. Store config/state/credentials outside the skill. Never copy credentials into examples or reference files.

## CLI

Resolve this skill directory, then run:

```bash
python3 <skill-dir>/scripts/ndb.py --config /absolute/path/config.json status
```

Python 3.11+ is required. The CLI uses the standard library. The optional credential `.env` adapter also requires `python-dotenv`.

Commands:

- `discover`: compact cluster/subnet/container inventory and supported VMM API version.
- `preflight`: cluster/subnet/container/network validation; run before a new VM, not against its occupied IP after deployment. Confirm the chosen IP has no conflict with IPAM/ARP on the target VLAN. A failed ping alone is insufficient.
- `verify-image`: compare the downloaded installer against the published checksum.
- `serve-image`: temporarily expose only the verified installer on the configured local interface. Run in a separate terminal; stop it as soon as the image import succeeds. No directory listing or credentials are served.
- `import-image`, `create-vm`, `power-on`: separate resumable stages. Inspect their saved task on failure; do not repeatedly resubmit.
- `credentials --account pc|pe|ndb`: terminal prompts; stores credentials owner-only without displaying them.
- `initialize`: first-login EULA/password setup, with generated password saved before the request. Run within authorized installation scope; if EULA acceptance was excluded, stop before this stage.
- `configure`: register the PE cluster, select database storage, and start required built-in software-profile setup. It does not automatically enable HA, telemetry, SMTP, or provision databases.
- `status`: compact setup operations, requested profile version states, registered clusters, database count. Add `--details` to list all profiles.
- `profiles`: after successful NDB setup, create/reuse configured compute and network profiles. It currently supports DHCP-managed database VLANs. This does not change the appliance's static IP.
- `rotate-password`: generate a new password, preserve a protected recovery candidate, change using `currentPassword`, and verify with a fresh session. Do not rerun an uncertain rotation; reconcile the protected candidate first. Never print credentials unless the user explicitly requests them.

For exact stage behavior, errors, TLS choices, and recovery, read [references/deployment.md](references/deployment.md). For profile limits read [references/profiles.md](references/profiles.md).

## Tool routing

Prefer Nutanix MCP tools when they expose compatible operations; fetch their schemas before use. The bundled CLI is a fallback when the server's API version is incompatible, an operation is absent, or resumable deterministic execution is needed. Do not change the MCP catalog just to deploy NDB.

For an installer, first reuse a verified local file. Otherwise confirm the latest compatible build and checksum through the authenticated Nutanix Support Portal. Use an available browser/computer-use tool for that download, following its skill. Login credentials for Prism Central, Prism Element, and Support Portal are separate. Do not extract browser cookies or credentials. The CLI does not implement portal sign-in/download.

Use API calls for deployment/configuration; use browser/console only for a missing API capability or diagnosis. An inaccessible browser is not a reason to rediscover the NDB UI's JavaScript APIs: the known calls are already in the scripts.

## Finish and report

Verify setup operation status `5` (success), requested profiles' version status `READY`, appliance static IP/HTTPS reachability, and absence of unintended database instances. Distinguish supporting profiles from software images containing SQL Server or pgvector. Report the URL, username, protected credential path, completed scope, and any specific source-image dependency; do not dump task logs or passwords.

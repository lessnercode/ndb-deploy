# NDB deployment skill

Deploy Nutanix Database Service on AHV with resumable API stages and local documentation. Prepare supporting database profiles without provisioning database instances.

Read [the skill instructions](SKILL.md) and [the deployment how-to](docs/how-to.md). Copy [the example config](references/config.example.json) into a separate working directory and replace all placeholders. Credentials and deployment state stay outside this repository.

Requires Python 3.11 or later. Run offline checks with:

```bash
python3 -m unittest discover -s scripts -p test_ndb.py
```

Validation covers mocked behavior and an existing deployment read-only status check; a fresh end-to-end deployment of this packaged version has not been performed.

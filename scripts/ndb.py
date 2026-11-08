#!/usr/bin/env python3
"""NDB deployment stages. Default output is compact JSON; secrets are never printed."""
import argparse
import base64
import getpass
import hashlib
import http.cookiejar
import http.server
import ipaddress
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Error(Exception):
    pass


def write_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_private(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise Error(f'Credential file must be owner-only: chmod 600 {path}')
    return json.loads(path.read_text())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Error('API redirect refused; configure the final API origin directly')


class API:
    def __init__(self, url, username, password, tls=None):
        if not url.startswith('https://'):
            raise Error('API URLs must use HTTPS')
        self.url = url.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode(f'{username}:{password}'.encode()).decode()
        tls = tls or {}
        context = (ssl._create_unverified_context() if tls.get('allow_self_signed')
                   else ssl.create_default_context(cafile=tls.get('ca_file')))
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

    def call(self, path, body=None, method=None, headers=None, auth=True, raw=False):
        h = {'Content-Type': 'application/json', 'client_tz': 'UTC'}
        if auth:
            h['Authorization'] = self.auth
        h.update(headers or {})
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=120) as response:
                content = response.read()
                result = json.loads(content) if content else None
                return (result, dict(response.headers)) if raw else result
        except urllib.error.HTTPError as exc:
            exc.close()
            # Server bodies may echo submitted passwords or session tokens: never print them.
            raise Error(f'HTTP {exc.code} at {path.split("?")[0]}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Error(f'Connection failed at {path.split("?")[0]}; inspect state before retrying a write') from None


class Runner:
    def __init__(self, config):
        self.c = config
        self.path = Path(config['state_file']).expanduser()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        binding = hashlib.sha256(json.dumps(config.get('deployment', {}), sort_keys=True).encode()).hexdigest()
        if self.state.get('binding', binding) != binding:
            raise Error('State belongs to different deployment inputs; use a separate state_file')
        self.state['binding'] = binding

    def save(self, **values):
        self.state.update(values)
        write_private(self.path, self.state)

    def credentials(self):
        p = self.c.get('credentials_file')
        return read_private(Path(p).expanduser()) if p and Path(p).expanduser().exists() else {}

    def api(self, account, password=None):
        section = self.c['prism' if account == 'pc' else 'ndb']
        cred = self.credentials()
        user = cred.get(account + '_username') or os.getenv(account.upper() + '_USERNAME')
        pw = password or cred.get(account + '_password') or os.getenv(account.upper() + '_PASSWORD')
        # Optional compatibility with an existing Nutanix MCP .env; never copy its secrets.
        if account == 'pc' and section.get('credentials_env_file'):
            try:
                from dotenv import dotenv_values
            except ImportError:
                raise Error('Use the MCP Python environment (python-dotenv), or set PC_USERNAME/PC_PASSWORD')
            source = Path(section['credentials_env_file']).expanduser()
            if source.stat().st_mode & 0o077:
                raise Error('MCP credential .env must be owner-only')
            env = dotenv_values(source)
            user, pw = user or env.get('PC_USERNAME'), pw or env.get('PC_PASSWORD')
        if not user or not pw:
            raise Error(f'Missing {account} credentials; use credentials --account {account}')
        return API(section['url'], user, pw, section.get('tls'))

    def ndb(self):
        api = self.api('ndb')
        api.call('/era/v0.9/auth/validate?token=true&expire=20')
        return api

    def v4_list(self, api, path):
        items, page = [], 0
        while True:
            response = api.call(path + ('&' if '?' in path else '?') + f'$limit=100&$page={page}')
            batch = response.get('data', [])
            if not isinstance(batch, list):
                raise Error('Expected a list from ' + path)
            items.extend(batch)
            total = response.get('metadata', {}).get('totalAvailableResults')
            if not batch or (total is not None and len(items) >= total) or (total is None and len(batch) < 100):
                return items
            page += 1

    def version(self, api):
        if self.state.get('vmm_version'):
            return self.state['vmm_version']
        for version in ['v4.3', 'v4.2', 'v4.0']:
            try:
                api.call('/api/vmm/' + version + '/content/images?$limit=1')
                # Discovery must not bind a new deployment before IDs are selected.
                self.state['vmm_version'] = version
                return version
            except Error as exc:
                if 'HTTP 404 ' not in str(exc):
                    raise
        raise Error('No supported VMM API version found')

    def discover(self):
        api = self.api('pc')
        v = self.version(api)
        return {'vmm_api': v, 'clusters': self.v4_list(api, '/api/clustermgmt/v4.0/config/clusters'),
                'subnets': self.v4_list(api, '/api/networking/v4.0/config/subnets'),
                'containers': self.v4_list(api, '/api/clustermgmt/v4.0/config/storage-containers')}

    def preflight(self):
        d = self.c['deployment']
        info = self.discover()
        subnet = next((s for s in info['subnets'] if s['extId'] == d['subnet_id']), None)
        if not subnet:
            raise Error('Configured subnet does not exist')
        clusters = subnet.get('clusterReferenceList', [subnet.get('clusterReference')])
        if d['cluster_id'] not in clusters:
            raise Error('Subnet does not belong to the chosen cluster')
        if subnet.get('subnetType') != 'VLAN':
            raise Error('This script supports VLAN deployment; use the overlay workflow for VPC subnets')
        cid = d['boot_container_id']
        if not any(s.get('containerExtId', s.get('extId')) == cid and s.get('clusterExtId') == d['cluster_id'] for s in info['containers']):
            raise Error('Boot container does not belong to the chosen cluster')
        net = ipaddress.ip_network(f"{d['static_ip']}/{d['prefix_length']}", strict=False)
        if ipaddress.ip_address(d['gateway']) not in net or ipaddress.ip_address(d['static_ip']) in [net.network_address, net.broadcast_address]:
            raise Error('Invalid static address or gateway')
        ip = subnet['ipConfig'][0]['ipv4']
        actual = ipaddress.ip_network(f"{ip['ipSubnet']['ip']['value']}/{ip['ipSubnet']['prefixLength']}")
        if net != actual or d['gateway'] != ip['defaultGatewayIp']['value']:
            raise Error('Static network settings disagree with Prism')
        for pool in ip.get('poolList', []):
            if ipaddress.ip_address(pool['startIp']['value']) <= ipaddress.ip_address(d['static_ip']) <= ipaddress.ip_address(pool['endIp']['value']):
                raise Error('Static address is inside DHCP pool; reserve/exclude it before deployment')
        api = self.api('pc')
        for suffix in ['vnics', 'reserved-ips']:
            rows = self.v4_list(api, '/api/networking/v4.0/config/subnets/' + d['subnet_id'] + '/' + suffix)
            if d['static_ip'] in json.dumps(rows):
                raise Error('Static address appears assigned or reserved; inspect its ownership')
        return {'cluster_id': d['cluster_id'], 'subnet': subnet['name'], 'network': str(net),
                'static_ip': d['static_ip'], 'remaining_check': 'Confirm no ARP response or conflicting IPAM allocation before creating the VM'}

    def verify_image(self):
        image = self.c['image']
        path = Path(image['file']).expanduser()
        if not path.is_file() or path.stat().st_size == 0:
            raise Error('Installer is missing or empty')
        algorithm = image.get('checksum_algorithm', 'sha256')
        if algorithm not in ['sha256', 'sha1', 'md5']:
            raise Error('Unsupported checksum algorithm')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, algorithm).hexdigest()
        if not secrets.compare_digest(digest.lower(), image['checksum'].lower()):
            raise Error('Installer checksum mismatch')
        self.save(verified_image={'path': str(path.resolve()), 'size': path.stat().st_size, 'checksum': digest})
        return {'verified': True, 'algorithm': algorithm, 'checksum': digest, 'bytes': path.stat().st_size}

    def mutation(self, label, fn, checkpoint=None):
        if self.state.get('pending'):
            raise Error('An earlier write has uncertain completion: ' + self.state['pending'] + '; inspect resources/tasks before retrying')
        self.save(pending=label)
        result = fn()
        self.save(pending=None, **(checkpoint(result) if checkpoint else {}))
        return result

    def wait_task(self, api, task_id, v3=False):
        path = '/api/nutanix/v3/tasks/' if v3 else '/api/prism/v4.0/config/tasks/'
        deadline = time.monotonic() + self.c.get('task_timeout_seconds', 1800)
        while time.monotonic() < deadline:
            result = api.call(path + urllib.parse.quote(task_id, safe=''))
            task = result if v3 else result['data']
            status = task.get('status')
            if status == 'SUCCEEDED':
                return task
            if status in ['FAILED', 'CANCELED', 'CANCELLED', 'ABORTED']:
                raise Error(f'Task {task_id} ended with {status}; inspect task and subtasks before another write')
            print(json.dumps({'task': task_id, 'status': status, 'percent': task.get('progressPercentage', task.get('percentage_complete'))}), flush=True)
            time.sleep(15)
        raise Error('Task still running; resume this command to poll the saved task')

    def import_image(self):
        api = self.api('pc')
        v = self.version(api)
        if self.state.get('image_task'):
            self.wait_task(api, self.state['image_task'])
        matches = [x for x in self.v4_list(api, '/api/vmm/' + v + '/content/images') if x['name'] == self.c['image']['name']]
        if matches:
            if len(matches) != 1 or matches[0].get('source', {}).get('url') != self.c['image']['source_url']:
                raise Error('Conflicting image name; inspect before adopting it')
            self.save(image_id=matches[0]['extId'])
            return {'image_id': matches[0]['extId'], 'reused': True}
        self.verify_image()
        body = {'name': self.c['image']['name'], 'type': 'DISK_IMAGE',
                'clusterLocationExtIds': [self.c['deployment']['cluster_id']],
                'source': {'$objectType': 'vmm.v4.content.UrlSource', 'url': self.c['image']['source_url']}}
        result = self.mutation('import-image', lambda: api.call('/api/vmm/' + v + '/content/images', body,
            headers={'Ntnx-Request-Id': str(uuid.uuid4())}),
            checkpoint=lambda r: {'image_task': r['data']['extId']})
        return self.import_image()

    def create_vm(self):
        api = self.api('pc')
        v = self.version(api)
        d = self.c['deployment']
        for key in ['static_ip', 'gateway', 'dns']:
            ipaddress.IPv4Address(d[key])
        if self.state.get('vm_task'):
            self.wait_task(api, self.state['vm_task'], v3=True)
        matches = [x for x in self.v4_list(api, '/api/vmm/' + v + '/ahv/config/vms') if x['name'] == d['name']]
        if matches:
            vm = matches[0]
            nic_ids = [n.get('nicNetworkInfo', n.get('networkInfo', {})).get('subnet', {}).get('extId') for n in vm.get('nics', [])]
            if len(matches) != 1 or vm.get('cluster', {}).get('extId') != d['cluster_id'] or d['subnet_id'] not in nic_ids or vm.get('numSockets') != d['sockets'] or vm.get('numCoresPerSocket') != d['cores_per_socket'] or vm.get('memorySizeBytes') != d['memory_gib'] * 1024**3:
                raise Error('Existing VM does not match deployment; inspect before adopting')
            self.save(vm_id=vm['extId'])
            return {'vm_id': vm['extId'], 'reused': True}
        image_id = self.state.get('image_id') or self.c['image'].get('id')
        if not image_id:
            raise Error('Import an image first')
        image = api.call('/api/vmm/' + v + '/content/images/' + image_id)['data']
        netmask = str(ipaddress.ip_network('0.0.0.0/' + str(d['prefix_length'])).netmask)
        script = '#cloud-config\nruncmd:\n- configure_static_ip ip={static_ip} gateway={gateway} netmask={netmask} nameserver={dns}\n'.format(**d, netmask=netmask)
        body = {'api_version': '3.1', 'metadata': {'kind': 'vm'}, 'spec': {
            'name': d['name'], 'cluster_reference': {'kind': 'cluster', 'uuid': d['cluster_id']},
            'resources': {'num_sockets': d['sockets'], 'num_vcpus_per_socket': d['cores_per_socket'],
                'memory_size_mib': d['memory_gib'] * 1024, 'power_state': 'OFF',
                'boot_config': {'boot_type': 'LEGACY', 'boot_device_order_list': ['DISK', 'CDROM', 'NETWORK']},
                'disk_list': [{'device_properties': {'device_type': 'DISK', 'disk_address': {'device_index': 0, 'adapter_type': 'SCSI'}},
                    'disk_size_bytes': image['sizeBytes'], 'data_source_reference': {'kind': 'image', 'uuid': image_id},
                    'storage_config': {'storage_container_reference': {'kind': 'storage_container', 'uuid': d['boot_container_id']}}}],
                'nic_list': [{'subnet_reference': {'kind': 'subnet', 'uuid': d['subnet_id']}, 'is_connected': True}],
                'guest_customization': {'cloud_init': {'user_data': base64.b64encode(script.encode()).decode()}}}}}
        result = self.mutation('create-vm', lambda: api.call('/api/nutanix/v3/vms', body),
            checkpoint=lambda r: {'vm_task': r['status']['execution_context']['task_uuid']})
        return self.create_vm()

    def power_on(self):
        api = self.api('pc')
        v = self.version(api)
        if self.state.get('power_task'):
            self.wait_task(api, self.state['power_task'])
        path = '/api/vmm/' + v + '/ahv/config/vms/' + self.state['vm_id']
        vm, headers = api.call(path, raw=True)
        if vm['data'].get('powerState') == 'ON':
            return {'power': 'ON'}
        etag = next(value for key, value in headers.items() if key.lower() == 'etag')
        result = self.mutation('power-on', lambda: api.call(path + '/$actions/power-on', method='POST',
            headers={'If-Match': etag, 'Ntnx-Request-Id': str(uuid.uuid4())}),
            checkpoint=lambda r: {'power_task': r['data']['extId']})
        return self.power_on()

    def initialize(self):
        # Calling this stage explicitly follows acceptance of installation/EULA scope.
        creds = self.credentials()
        if creds.get('ndb_password'):
            self.ndb()
            if self.state.get('pending') == 'initialize-admin':
                self.save(pending=None)
            return {'initialized': True, 'changed': False}
        api = API(self.c['ndb']['url'], 'admin', '********', self.c['ndb'].get('tls'))
        try:
            api.call('/era/v0.9/auth/validate?token=true&expire=20')
        except Error as exc:
            if not any(f'HTTP {code} ' in str(exc) for code in [402, 403]):
                raise
        else:
            return {'initialized': True, 'changed': False}
        creds['ndb_username'] = 'admin'
        creds['ndb_password'] = 'Ndb!' + secrets.token_urlsafe(24) + '9aA'
        # Save before write so a lost response cannot lose the generated password.
        write_private(self.c['credentials_file'], creds)
        self.mutation('initialize-admin', lambda: api.call('/era/v0.9/auth/update?accept-eula=true',
            {'password': creds['ndb_password']}, auth=False))
        self.ndb()
        return {'initialized': True, 'credentials_file': self.c['credentials_file']}

    def rotate_password(self):
        api = self.ndb()
        creds = self.credentials()
        pending = Path(self.c['credentials_file'] + '.pending')
        if pending.exists():
            raise Error('Pending credential rotation exists; verify which password works before another rotation')
        candidate = dict(creds)
        candidate['ndb_password'] = 'Ndb!' + secrets.token_urlsafe(24) + '9aA'
        write_private(pending, candidate)
        self.mutation('rotate-password', lambda: api.call('/era/v0.9/auth/update?accept-eula=true',
            {'password': candidate['ndb_password'], 'currentPassword': creds['ndb_password']}))
        write_private(self.c['credentials_file'], candidate)
        self.ndb()  # New connection and cookies: prove the new password works.
        pending.unlink()
        return {'password_rotated': True, 'credentials_file': self.c['credentials_file']}

    def configure(self):
        api = self.ndb()
        cred = self.credentials()
        d = self.c['deployment']
        name = self.c['cluster_name']
        clusters = api.call('/era/v0.9/clusters')
        found = [c for c in clusters if c['name'] == name]
        if found:
            if len(found) != 1 or self.c['pe_address'] not in found[0].get('ipAddresses', []):
                raise Error('Conflicting registered cluster name')
            cid = found[0]['id']
            storage = next((p['value'] for p in found[0].get('properties', [])
                            if p['name'] == 'ERA_STORAGE_CONTAINER'), None)
            if storage and storage != self.c['database_container']:
                raise Error('Existing cluster uses another database container; inspect before changing it')
            if storage:
                self.save(ndb_cluster_id=cid, connection_uploaded=True)
                # Never restart an existing cluster's setup merely because local state was lost.
                if not self.state.get('database_container_selected') or self.state.get('setup_started'):
                    return {'setup': 'existing_storage_configured', 'cluster_id': cid,
                            'next': 'Run status and inspect its existing setup operation'}
        else:
            if not cred.get('pe_username') or not cred.get('pe_password'):
                raise Error('Set Prism Element credentials first')
            result = self.mutation('register-cluster', lambda: api.call('/era/v0.9/clusters', {
                'name': name, 'description': 'NDB cluster', 'ipAddresses': [self.c['pe_address']],
                'username': cred['pe_username'], 'password': cred['pe_password'],
                'status': 'UP', 'version': 'v2', 'cloudType': 'NTNX'}),
                checkpoint=lambda r: {'ndb_cluster_id': r['id']})
            cid = result['id']
        self.save(ndb_cluster_id=cid)
        connection = {'protocol': 'https', 'port': '9440', 'ip_address': self.c['pe_address'],
                      'creds_bag': {'username': cred['pe_username'], 'password': cred['pe_password']}}
        def upload(payload, query):
            boundary = 'NdbBoundary' + uuid.uuid4().hex
            data = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"\r\n\r\n' + json.dumps(payload) + '\r\n--' + boundary + '--\r\n').encode()
            return api.call('/era/v0.9/clusters/' + cid + '/json?' + query, data,
                headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        if not self.state.get('connection_uploaded'):
            self.mutation('upload-cluster-connection', lambda: upload(connection, 'skip_upload=false&skip_profile=true&updateJson=true'),
                checkpoint=lambda r: {'connection_uploaded': True})
            self.save(connection_uploaded=True)
        if self.state.get('setup_started'):
            return {'setup': 'already_started', 'cluster_id': cid}
        containers = api.call('/era/v0.9/clusters/i/' + cid + '/storage-containers')['entities']
        if self.c['database_container'] not in [c['name'] for c in containers]:
            raise Error('Database storage container not found')
        body = {'name': name, 'ipAddresses': [self.c['pe_address']], 'status': 'UP', 'version': 'v2',
                'cloudType': 'NTNX', 'properties': [{'name': 'ERA_STORAGE_CONTAINER', 'value': self.c['database_container']}]}
        self.mutation('set-database-container', lambda: api.call('/era/v0.9/clusters/' + cid, body, method='PUT'),
            checkpoint=lambda r: {'database_container_selected': True})
        stored = api.call('/era/v0.9/clusters/i/' + cid + '/json')
        stored = json.loads(stored) if isinstance(stored, str) else stored
        stored['clusterIp'] = self.c['pe_address']
        self.mutation('start-setup', lambda: upload(stored, 'skip_upload=true&skip_profile=false&updateJson=false'),
            checkpoint=lambda r: {'setup_started': True})
        self.save(setup_started=True)
        return {'setup': 'started', 'cluster_id': cid}

    def status(self, details=False):
        api = self.ndb()
        operations = api.call('/era/v0.9/operations/short-info?hide-subops=true&user-triggered=true&system-triggered=true&days=2&time-zone=UTC')
        all_profiles = api.call('/era/v0.9/profiles')
        selected = {target.get(kind + '_name', target['label'] + '_' + kind.upper())
                    for target in self.c.get('profiles', []) for kind in ['compute', 'network']}
        shown = all_profiles if details else [p for p in all_profiles if p['name'] in selected]
        counts = {kind: sum(p['type'] == kind for p in all_profiles) for kind in sorted({p['type'] for p in all_profiles})}
        return {'url': self.c['ndb']['url'], 'profile_counts': counts, 'clusters': [{'id': c['id'], 'name': c['name'], 'status': c['status']} for c in api.call('/era/v0.9/clusters')],
                'operations': [{'id': o['id'], 'name': o['name'], 'status': o['status'], 'percent': o.get('percentageComplete')} for o in operations.get('operations', [])],
                'profiles': [{'id': p['id'], 'name': p['name'], 'type': p['type'], 'engine': p.get('engineType'), 'versions': [v.get('status') for v in p.get('versions', [])]} for p in shown],
                'database_count': len(api.call('/era/v0.9/databases'))}

    def profiles(self):
        api = self.ndb()
        ops = self.status()['operations']
        setup = [o for o in ops if o['name'] == 'Configure OOB software profiles']
        if not setup or any(o['status'] != '5' for o in setup):
            raise Error('NDB setup must finish successfully before creating network profiles; inspect status')
        subnet = self.c['subnet_name']
        if self.c.get('database_network_type', 'DHCP') != 'DHCP':
            raise Error('Static DB pools require explicit IPAM configuration; follow the network reference')
        vlans = api.call('/era/v0.9/resources/networks?detailed=true')
        if not any(v['name'] == subnet for v in vlans):
            self.mutation('register-vlan', lambda: api.call('/era/v0.9/resources/networks',
                {'name': subnet, 'type': self.c.get('database_network_type', 'DHCP'), 'properties': [], 'ipPools': []}))
        existing = api.call('/era/v0.9/profiles')
        results = []
        for target in self.c.get('profiles', []):
            engine, label = target['engine'], target['label']
            for kind, suffix, props, profile_engine in [
                ('Network', '_NETWORK', [{'name': 'VLAN_NAME', 'value': subnet}], engine),
                ('Compute', '_COMPUTE', [{'name': 'CPUS', 'value': str(target.get('sockets', 1))}, {'name': 'CORE_PER_CPU', 'value': str(target.get('cores_per_socket', 4))}, {'name': 'MEMORY_SIZE', 'value': str(target.get('memory_gib', 32))}], 'Generic')]:
                name = target.get(kind.lower() + '_name', label + suffix)
                matches = [p for p in existing if p['name'] == name]
                if matches:
                    p = matches[0]
                    versions = p.get('versions', [])
                    expected = {x['name']: x['value'] for x in props}
                    actual = {x['name']: x['value'] for x in versions[-1].get('properties', [])} if versions else {}
                    if len(matches) != 1 or p['type'] != kind or p.get('engineType') != profile_engine or any(actual.get(k) != value for k, value in expected.items()):
                        raise Error('Conflicting existing profile: ' + name)
                else:
                    self.mutation('create-profile-' + name, lambda: api.call('/era/v0.9/profiles',
                        {'name': name, 'type': kind, 'engineType': profile_engine, 'published': True,
                         'description': 'Supporting profile; software image readiness is separate',
                         'topology': 'ALL', 'properties': props}))
                results.append(name)
        return {'supporting_profiles': results, 'software_images': 'SQL Server and pgvector require registered source images; no database instances created'}


def serve(config):
    image = Path(config['image']['file']).expanduser().resolve()
    url = urllib.parse.urlparse(config['image']['source_url'])
    if url.scheme != 'http' or urllib.parse.unquote(url.path) != '/' + image.name:
        raise Error('serve-image requires an HTTP source_url whose path is the installer filename')
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if urllib.parse.urlparse(self.path).path != url.path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Length', str(image.stat().st_size))
            self.end_headers()
            if self.command != 'HEAD':
                import shutil
                with image.open('rb') as stream:
                    shutil.copyfileobj(stream, self.wfile)
        do_HEAD = do_GET
        def log_message(self, *args):
            pass
    print(json.dumps({'serving': image.name, 'bind': url.hostname, 'port': url.port or 80}), flush=True)
    with http.server.ThreadingHTTPServer((url.hostname, url.port or 80), Handler) as server:
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    commands = ['discover', 'preflight', 'verify-image', 'serve-image', 'import-image', 'create-vm', 'power-on', 'initialize', 'configure', 'status', 'profiles', 'credentials', 'rotate-password']
    parser.add_argument('command', choices=commands)
    parser.add_argument('--account', choices=['pc', 'pe', 'ndb'])
    parser.add_argument('--details', action='store_true', help='Include all profiles in status output')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    runner = Runner(config)
    if args.command == 'credentials':
        if not args.account:
            parser.error('credentials requires --account')
        cred = runner.credentials()
        cred[args.account + '_username'] = input('Username: ').strip()
        cred[args.account + '_password'] = getpass.getpass('Password: ')
        if not cred[args.account + '_username'] or not cred[args.account + '_password']:
            raise Error('Empty credentials were not saved')
        write_private(config['credentials_file'], cred)
        result = {'credentials_saved': args.account}
    elif args.command == 'serve-image':
        runner.verify_image()
        serve(config)
        return
    else:
        result = runner.status(details=args.details) if args.command == 'status' else getattr(runner, args.command.replace('-', '_'))()
        if args.command == 'discover':
            result = {k: ([{key: item.get(key) for key in ['name', 'extId', 'containerExtId', 'clusterExtId', 'clusterReference', 'subnetType']} for item in value] if isinstance(value, list) else value) for k, value in result.items()}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (Error, KeyError, ValueError, FileNotFoundError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        sys.exit(1)

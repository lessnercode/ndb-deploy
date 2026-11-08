"""Offline behavioral checks: no Prism/NDB writes or real network requests."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

import ndb


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'state_file': str(self.root / 'state.json'),
            'credentials_file': str(self.root / 'credentials.json'),
            'deployment': {'name': 'NDB-test', 'cluster_id': 'cluster', 'subnet_id': 'subnet',
                'boot_container_id': 'source-container', 'static_ip': '192.0.2.140',
                'prefix_length': 25, 'gateway': '192.0.2.129', 'dns': '192.0.2.6',
                'sockets': 8, 'cores_per_socket': 2, 'memory_gib': 16},
            'ndb': {'url': 'https://192.0.2.140'}, 'prism': {'url': 'https://pc.example'},
            'subnet_name': 'secondary', 'profiles': [{'label': 'SQL', 'engine': 'sqlserver_database'}],
            'image': {'id': 'image', 'name': 'NDB-image', 'file': str(self.root / 'image.qcow2'),
                      'source_url': 'http://192.0.2.240:8765/image.qcow2'}}
        self.runner = ndb.Runner(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_credentials_permissions_and_atomic_replacement(self):
        path = self.config['credentials_file']
        ndb.write_private(path, {'ndb_password': 'first'})
        ndb.write_private(path, {'ndb_password': 'second'})
        self.assertEqual(ndb.read_private(path)['ndb_password'], 'second')
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        Path(path).chmod(0o644)
        with self.assertRaises(ndb.Error):
            ndb.read_private(path)

    def test_interrupted_write_blocks_another_write(self):
        def lost_response():
            raise ndb.Error('Connection failed')
        with self.assertRaises(ndb.Error):
            self.runner.mutation('create-vm', lost_response)
        reloaded = ndb.Runner(self.config)
        fn = Mock()
        with self.assertRaises(ndb.Error):
            reloaded.mutation('create-vm', fn)
        fn.assert_not_called()
        self.assertEqual(reloaded.state['pending'], 'create-vm')

    def test_response_checkpoint_saved_with_pending_cleared(self):
        self.runner.mutation('create-vm', lambda: {'task': 'saved-task'},
            checkpoint=lambda result: {'vm_task': result['task']})
        reloaded = ndb.Runner(self.config)
        self.assertIsNone(reloaded.state['pending'])
        self.assertEqual(reloaded.state['vm_task'], 'saved-task')

    def test_changed_deployment_cannot_reuse_state(self):
        self.runner.save(vm_id='existing')
        other = copy.deepcopy(self.config)
        other['deployment']['static_ip'] = '192.0.2.141'
        with self.assertRaises(ndb.Error):
            ndb.Runner(other)

    def test_server_error_cannot_echo_secret(self):
        api = ndb.API('https://pc.example', 'admin', 'sensitive-password')
        api.opener = Mock()
        api.opener.open.side_effect = urllib.error.HTTPError(
            'https://pc.example/test', 400, 'Bad Request', {}, io.BytesIO(b'sensitive-password'))
        with self.assertRaises(ndb.Error) as context:
            api.call('/test', {'password': 'sensitive-password'})
        self.assertNotIn('sensitive-password', str(context.exception))

    def test_redirect_rejected(self):
        with self.assertRaises(ndb.Error):
            ndb.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example')

    def test_checksum_failure_does_not_mark_verified(self):
        Path(self.config['image']['file']).write_bytes(b'incomplete image')
        self.config['image'].update(checksum_algorithm='sha256', checksum='0' * 64)
        with self.assertRaises(ndb.Error):
            self.runner.verify_image()
        self.assertNotIn('verified_image', self.runner.state)

    def test_saved_image_task_is_polled_before_reuse(self):
        self.runner.save(image_task='task')
        api = Mock()
        self.runner.api = Mock(return_value=api)
        self.runner.version = Mock(return_value='v4.0')
        self.runner.wait_task = Mock()
        self.runner.v4_list = Mock(return_value=[{'extId': 'image', 'name': 'NDB-image',
            'source': {'url': self.config['image']['source_url']}}])
        self.assertEqual(self.runner.import_image()['image_id'], 'image')
        self.runner.wait_task.assert_called_once_with(api, 'task')
        api.call.assert_not_called()

    def test_vm_payload_and_resume_without_duplicate_create(self):
        api = Mock()
        api.call.side_effect = [
            {'data': {'sizeBytes': 100 * 1024**3}},
            {'status': {'execution_context': {'task_uuid': 'vm-task'}}}]
        self.runner.api = Mock(return_value=api)
        self.runner.version = Mock(return_value='v4.0')
        self.runner.wait_task = Mock()
        d = self.config['deployment']
        vm = {'extId': 'vm', 'name': d['name'], 'cluster': {'extId': d['cluster_id']},
              'numSockets': 8, 'numCoresPerSocket': 2, 'memorySizeBytes': 16 * 1024**3,
              'nics': [{'nicNetworkInfo': {'subnet': {'extId': d['subnet_id']}}}]}
        self.runner.v4_list = Mock(side_effect=[[], [vm]])
        self.runner.create_vm()
        body = api.call.call_args_list[1].args[1]
        resources = body['spec']['resources']
        self.assertEqual(resources['boot_config']['boot_device_order_list'], ['DISK', 'CDROM', 'NETWORK'])
        self.assertEqual(resources['disk_list'][0]['disk_size_bytes'], 100 * 1024**3)
        self.assertEqual(resources['disk_list'][0]['storage_config']['storage_container_reference']['uuid'], 'source-container')
        self.assertEqual(self.runner.state['vm_id'], 'vm')
        api.call.reset_mock()
        self.runner.v4_list = Mock(return_value=[vm])
        self.runner.create_vm()
        api.call.assert_not_called()

    def test_existing_initialized_admin_does_not_reset_password(self):
        self.runner.credentials = Mock(return_value={'ndb_password': 'existing'})
        self.runner.ndb = Mock()
        with patch.object(ndb, 'API') as api:
            self.runner.initialize()
            api.assert_not_called()

    def test_password_rotation_updates_only_after_success(self):
        ndb.write_private(self.config['credentials_file'], {'ndb_username': 'admin', 'ndb_password': 'old'})
        api = Mock()
        self.runner.ndb = Mock(return_value=api)
        self.runner.rotate_password()
        stored = ndb.read_private(self.config['credentials_file'])
        body = api.call.call_args.args[1]
        self.assertEqual(body['currentPassword'], 'old')
        self.assertEqual(body['password'], stored['ndb_password'])
        self.assertNotEqual(stored['ndb_password'], 'old')
        self.assertFalse(Path(self.config['credentials_file'] + '.pending').exists())
        self.assertEqual(self.runner.ndb.call_count, 2)

    def test_password_lost_response_keeps_recovery_candidate(self):
        ndb.write_private(self.config['credentials_file'], {'ndb_username': 'admin', 'ndb_password': 'old'})
        api = Mock()
        api.call.side_effect = ndb.Error('Connection failed')
        self.runner.ndb = Mock(return_value=api)
        with self.assertRaises(ndb.Error):
            self.runner.rotate_password()
        self.assertEqual(ndb.read_private(self.config['credentials_file'])['ndb_password'], 'old')
        candidate = ndb.read_private(self.config['credentials_file'] + '.pending')
        self.assertNotEqual(candidate['ndb_password'], 'old')
        self.assertEqual(self.runner.state['pending'], 'rotate-password')

    def test_discovery_version_probe_does_not_bind_placeholder_inputs(self):
        api = Mock()
        api.call.return_value = {'data': []}
        self.runner.version(api)
        self.assertFalse(Path(self.config['state_file']).exists())
        changed = copy.deepcopy(self.config)
        changed['deployment']['cluster_id'] = 'selected-after-discovery'
        ndb.Runner(changed)

    def test_version_negotiates_only_on_not_found(self):
        api = Mock()
        api.call.side_effect = [ndb.Error('HTTP 404 at /images'), {'data': []}]
        self.assertEqual(self.runner.version(api), 'v4.2')
        other = ndb.Runner({**self.config, 'state_file': str(self.root / 'other-state.json')})
        api.call.reset_mock()
        api.call.side_effect = ndb.Error('HTTP 401 at /images')
        with self.assertRaises(ndb.Error):
            other.version(api)
        self.assertEqual(api.call.call_count, 1)

    def test_configure_does_not_restart_existing_installation(self):
        self.config.update(cluster_name='cluster-name', pe_address='192.0.2.37', database_container='ndb')
        api = Mock()
        api.call.return_value = [{'id': 'registered', 'name': 'cluster-name',
            'ipAddresses': ['192.0.2.37'],
            'properties': [{'name': 'ERA_STORAGE_CONTAINER', 'value': 'ndb'}]}]
        self.runner.ndb = Mock(return_value=api)
        self.runner.credentials = Mock(return_value={})
        self.runner.configure()
        self.assertEqual(api.call.call_count, 1)
        self.assertEqual(api.call.call_args.args, ('/era/v0.9/clusters',))

    def test_supporting_profiles_reused_and_conflict_rejected(self):
        network = {'name': 'SQL_NETWORK', 'type': 'Network', 'engineType': 'sqlserver_database',
                   'versions': [{'properties': [{'name': 'VLAN_NAME', 'value': 'secondary'}]}]}
        compute = {'name': 'SQL_COMPUTE', 'type': 'Compute', 'engineType': 'Generic',
                   'versions': [{'properties': [{'name': 'CPUS', 'value': '1'},
                   {'name': 'CORE_PER_CPU', 'value': '4'}, {'name': 'MEMORY_SIZE', 'value': '32'}]}]}
        api = Mock()
        api.call.side_effect = [[{'name': 'secondary'}], [network, compute]]
        self.runner.ndb = Mock(return_value=api)
        self.runner.status = Mock(return_value={'operations': [{'name': 'Configure OOB software profiles', 'status': '5'}]})
        self.runner.profiles()
        self.assertTrue(all(len(c.args) == 1 for c in api.call.call_args_list))
        compute['versions'][0]['properties'][-1]['value'] = '8'
        api.call.side_effect = [[{'name': 'secondary'}], [network, compute]]
        with self.assertRaises(ndb.Error):
            self.runner.profiles()


if __name__ == '__main__':
    unittest.main()

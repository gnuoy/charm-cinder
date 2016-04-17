#!/usr/bin/python

import amulet
import os
import yaml
import time
import json
import subprocess

from charmhelpers.contrib.openstack.amulet.deployment import (
    OpenStackAmuletDeployment
)

from charmhelpers.contrib.openstack.amulet.utils import (
    OpenStackAmuletUtils,
    DEBUG,
    # ERROR
)

# Use DEBUG to turn on debug logging
u = OpenStackAmuletUtils(DEBUG)


class CinderBasicDeployment(OpenStackAmuletDeployment):
    """Amulet tests on a basic lvm-backed cinder deployment. Verify
       relations, service status, users and endpoint service catalog.
       Create, clone, delete volumes. Create volume from glance image.
       Create volume snapshot. Create volume from snapshot."""

    def __init__(self, series=None, openstack=None, source=None, git=False,
                 stable=False):
        """Deploy the entire test environment."""
        super(CinderBasicDeployment, self).__init__(series, openstack, source,
                                                    stable)
        self.git = git
        self._add_services()
        self._add_relations()
        self._configure_services()
        self._deploy()

        u.log.info('Waiting on extended status checks...')
        exclude_services = ['mysql']
        self._auto_wait_for_status(exclude_services=exclude_services)

        self._initialize_tests()

    def _add_services(self):
        """Add services

           Add the services that we're testing, where cinder is local,
           and the rest of the service are from lp branches that are
           compatible with the local charm (e.g. stable or next).
           """
        this_service = {'name': 'cinder'}
        other_services = [{'name': 'mysql'}, {'name': 'rabbitmq-server'},
                          {'name': 'keystone'}, {'name': 'glance'}]
        super(CinderBasicDeployment, self)._add_services(this_service,
                                                         other_services)

    def _add_relations(self):
        """Add relations for the services."""
        relations = {
            'keystone:shared-db': 'mysql:shared-db',
            'cinder:shared-db': 'mysql:shared-db',
            'cinder:identity-service': 'keystone:identity-service',
            'cinder:amqp': 'rabbitmq-server:amqp',
            'cinder:image-service': 'glance:image-service',
            'glance:identity-service': 'keystone:identity-service',
            'glance:shared-db': 'mysql:shared-db',
            'glance:amqp': 'rabbitmq-server:amqp'
        }
        super(CinderBasicDeployment, self)._add_relations(relations)

    def _configure_services(self):
        """Configure all of the services."""
        cinder_config = {'block-device': 'vdb',
                         'glance-api-version': '2',
                         'overwrite': 'true',
                         'ephemeral-unmount': '/mnt'}
        if self.git:
            amulet_http_proxy = os.environ.get('AMULET_HTTP_PROXY')

            reqs_repo = 'git://github.com/openstack/requirements'
            cinder_repo = 'git://github.com/openstack/cinder'
            if self._get_openstack_release() == self.trusty_icehouse:
                reqs_repo = 'git://github.com/coreycb/requirements'
                cinder_repo = 'git://github.com/coreycb/cinder'

            branch = 'stable/' + self._get_openstack_release_string()

            openstack_origin_git = {
                'repositories': [
                    {'name': 'requirements',
                     'repository': reqs_repo,
                     'branch': branch},
                    {'name': 'cinder',
                     'repository': cinder_repo,
                     'branch': branch},
                ],
                # Most tests use /mnt/openstack-git but cinder's using /dev/vdb
                # to store block devices so leave /mnt alone.
                'directory': '/tmp/openstack-git',
                'http_proxy': amulet_http_proxy,
                'https_proxy': amulet_http_proxy,
            }
            cinder_config['openstack-origin-git'] = \
                yaml.dump(openstack_origin_git)

        keystone_config = {'admin-password': 'openstack',
                           'admin-token': 'ubuntutesting'}
        mysql_config = {'dataset-size': '50%'}
        configs = {'cinder': cinder_config,
                   'keystone': keystone_config,
                   'mysql': mysql_config}
        super(CinderBasicDeployment, self)._configure_services(configs)

    def _initialize_tests(self):
        """Perform final initialization before tests get run."""
        # Access the sentries for inspecting service units
        self.cinder_sentry = self.d.sentry.unit['cinder/0']
        self.glance_sentry = self.d.sentry.unit['glance/0']
        self.mysql_sentry = self.d.sentry.unit['mysql/0']
        self.keystone_sentry = self.d.sentry.unit['keystone/0']
        self.rabbitmq_sentry = self.d.sentry.unit['rabbitmq-server/0']
        u.log.debug('openstack release val: {}'.format(
            self._get_openstack_release()))
        u.log.debug('openstack release str: {}'.format(
            self._get_openstack_release_string()))

        # Authenticate admin with keystone
        self.keystone = u.authenticate_keystone_admin(self.keystone_sentry,
                                                      user='admin',
                                                      password='openstack',
                                                      tenant='admin')

        # Authenticate admin with cinder endpoint
        self.cinder = u.authenticate_cinder_admin(self.keystone_sentry,
                                                  username='admin',
                                                  password='openstack',
                                                  tenant='admin')

        # Authenticate admin with glance endpoint
        self.glance = u.authenticate_glance_admin(self.keystone)

    def _run_action(self, unit_id, action, *args):
        command = ["juju", "action", "do", "--format=json", unit_id, action]
        command.extend(args)
        print("Running command: %s\n" % " ".join(command))
        output = subprocess.check_output(command)
        output_json = output.decode(encoding="UTF-8")
        data = json.loads(output_json)
        action_id = data[u'Action queued with id']
        return action_id

    def _wait_on_action(self, action_id):
        command = ["juju", "action", "fetch", "--format=json", action_id]
        while True:
            try:
                output = subprocess.check_output(command)
            except Exception as e:
                print(e)
                return False
            output_json = output.decode(encoding="UTF-8")
            data = json.loads(output_json)
            if data[u"status"] == "completed":
                return True
            elif data[u"status"] == "failed":
                return False
            time.sleep(2)

    def _extend_cinder_volume(self, vol_id, new_size=2):
        """Extend an existing cinder volume size.

        :param vol_id: existing cinder volume to extend
        :param new_size: new size in gigabytes
        :returns: None if successful; Failure message otherwise
        """
        # Extend existing volume size
        try:
            self.cinder.volumes.extend(vol_id, new_size)
            vol_size_org = self.cinder.volumes.get(vol_id).size
        except Exception as e:
            msg = 'Failed to extend volume: {}'.format(e)
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm that the volume reaches available status.
        ret = u.resource_reaches_status(self.cinder.volumes, vol_id,
                                        expected_stat="available",
                                        msg="Volume status wait")
        if not ret:
            msg = ('Cinder volume failed to reach expected state '
                   'while extending.')
            return ret

        # Validate volume size and status
        u.log.debug('Validating volume attributes...')
        vol_size_ext = self.cinder.volumes.get(vol_id).size
        vol_stat = self.cinder.volumes.get(vol_id).status
        msg_attr = ('Volume attributes - orig size:{} extended size:{} '
                    'stat:{}'.format(vol_size_org, vol_size_ext, vol_stat))

        if vol_size_ext > vol_size_org and vol_stat == 'available':
            u.log.debug(msg_attr)
        else:
            msg = ('Volume validation failed, {}'.format(msg_attr))
            return ret

        return None

    def _snapshot_cinder_volume(self, name='demo-snapshot', vol_id=None):
        """Create a snapshot of an existing cinder volume.

        :param name: display name to assign to snapshot
        :param vol_id: existing cinder volume to snapshot
        :returns: None if successful; Failure message otherwise
        """
        u.log.debug('Creating snapshot of volume ({})...'.format(vol_id))
        # Create snapshot of an existing cinder volume
        try:
            snap_new = self.cinder.volume_snapshots.create(
                volume_id=vol_id, display_name=name)
            snap_id = snap_new.id
        except Exception as e:
            msg = 'Failed to snapshot the volume: {}'.format(e)
            amulet.raise_status(amulet.FAIL, msg=msg)

        # Confirm that the volume reaches available status.
        ret = u.resource_reaches_status(self.cinder.volume_snapshots,
                                        snap_id,
                                        expected_stat="available",
                                        msg="Volume status wait")
        if not ret:
            msg = ('Cinder volume failed to reach expected state '
                   'while snapshotting.')
            return ret

        # Validate snapshot
        u.log.debug('Validating snapshot attributes...')
        snap_name = self.cinder.volume_snapshots.get(snap_id).display_name
        snap_stat = self.cinder.volume_snapshots.get(snap_id).status
        snap_vol_id = self.cinder.volume_snapshots.get(snap_id).volume_id
        msg_attr = ('Snapshot attributes - name:{} status:{} '
                    'vol_id:{}'.format(snap_name, snap_stat, snap_vol_id))

        if snap_name == name and snap_stat == 'available' \
                and snap_vol_id == vol_id:
            u.log.debug(msg_attr)
        else:
            msg = ('Snapshot validation failed, {}'.format(msg_attr))
            amulet.raise_status(amulet.FAIL, msg=msg)

        return snap_new

    def _check_cinder_lvm(self):
        """Inspect lvm on cinder unit, do basic validation against
        cinder volumes and snapshots that exist."""
        u.log.debug('Checking cinder volumes against lvm volumes...')
        # Inspect
        cmd = 'sudo lvs | grep cinder-volumes | awk \'{ print $1 }\''
        output, code = self.cinder_sentry.run(cmd)
        u.log.debug('{} `{}` returned '
                    '{}'.format(self.cinder_sentry.info['unit_name'],
                                cmd, code))
        if code != 0:
            return "command `{}` returned {}".format(cmd, str(code))

        vol_list = self.cinder.volumes.list()
        lv_id_list = output.split('\n')
        lv_count = len(lv_id_list)
        vol_count = len(vol_list)
        snap_count = len(self.cinder.volume_snapshots.list())

        # Expect cinder vol + snap count to match lvm log vol count
        u.log.debug('vols:{} snaps:{} lvs:{}'.format(vol_count,
                                                     snap_count,
                                                     lv_count))
        if (vol_count + snap_count) != len(lv_id_list):
            msg = ('lvm volume count ({}) != cinder volume + snap count '
                   '({})'.format(len(vol_list), len(lv_id_list)))
            return msg

        # Expect all cinder vol IDs to exist in the LVM volume list
        for vol_this in vol_list:
            try:
                vol_id = vol_this.id
                vol_name = vol_this.display_name
                lv_id = 'volume-{}'.format(vol_id)
                _index = lv_id_list.index(lv_id)
                u.log.info('Volume ({}) correlates to lv '
                           '{} ({})'.format(vol_name,
                                            _index,
                                            lv_id))
            except:
                u.log.error('lvs output: {}'.format(output))
                msg = ('Volume ID {} not found in '
                       'LVM volume list.'.format(vol_this.id))
                return msg

        return None

#    def test_100_services(self):
#        """Verify that the expected services are running on the
#           corresponding service units."""
#        services = {
#            self.cinder_sentry: ['cinder-api',
#                                 'cinder-scheduler',
#                                 'cinder-volume'],
#            self.glance_sentry: ['glance-registry',
#                                 'glance-api'],
#            self.mysql_sentry: ['mysql'],
#            self.keystone_sentry: ['keystone'],
#            self.rabbitmq_sentry: ['rabbitmq-server']
#        }
#        ret = u.validate_services_by_name(services)
#        if ret:
#            amulet.raise_status(amulet.FAIL, msg=ret)
#
#    def test_110_users(self):
#        """Verify expected users."""
#        u.log.debug('Checking keystone users...')
#        user0 = {'name': 'cinder_cinderv2',
#                 'enabled': True,
#                 'tenantId': u.not_null,
#                 'id': u.not_null,
#                 'email': 'juju@localhost'}
#        user1 = {'name': 'admin',
#                 'enabled': True,
#                 'tenantId': u.not_null,
#                 'id': u.not_null,
#                 'email': 'juju@localhost'}
#        user2 = {'name': 'glance',
#                 'enabled': True,
#                 'tenantId': u.not_null,
#                 'id': u.not_null,
#                 'email': 'juju@localhost'}
#        expected = [user0, user1, user2]
#        actual = self.keystone.users.list()
#
#        ret = u.validate_user_data(expected, actual)
#        if ret:
#            amulet.raise_status(amulet.FAIL, msg=ret)
#
#    def test_112_service_catalog(self):
#        """Verify that the service catalog endpoint data"""
#        u.log.debug('Checking keystone service catalog...')
#        endpoint_vol = {'adminURL': u.valid_url,
#                        'region': 'RegionOne',
#                        'publicURL': u.valid_url,
#                        'internalURL': u.valid_url}
#        endpoint_id = {'adminURL': u.valid_url,
#                       'region': 'RegionOne',
#                       'publicURL': u.valid_url,
#                       'internalURL': u.valid_url}
#        if self._get_openstack_release() >= self.trusty_icehouse:
#            endpoint_vol['id'] = u.not_null
#            endpoint_id['id'] = u.not_null
#
#        expected = {'image': [endpoint_id],
#                    'identity': [endpoint_id],
#                    'volume': [endpoint_id]}
#        actual = self.keystone.service_catalog.get_endpoints()
#
#        ret = u.validate_svc_catalog_endpoint_data(expected, actual)
#        if ret:
#            amulet.raise_status(amulet.FAIL, msg=ret)
#
#    def test_114_cinder_endpoint(self):
#        """Verify the cinder endpoint data."""
#        u.log.debug('Checking cinder endpoint...')
#        endpoints = self.keystone.endpoints.list()
#        admin_port = internal_port = public_port = '8776'
#        expected = {'id': u.not_null,
#                    'region': 'RegionOne',
#                    'adminurl': u.valid_url,
#                    'internalurl': u.valid_url,
#                    'publicurl': u.valid_url,
#                    'service_id': u.not_null}
#
#        ret = u.validate_endpoint_data(endpoints, admin_port, internal_port,
#                                       public_port, expected)
#        if ret:
#            amulet.raise_status(amulet.FAIL,
#                                msg='cinder endpoint: {}'.format(ret))
#
#    def test_202_cinder_glance_image_service_relation(self):
#        """Verify the cinder:glance image-service relation data"""
#        u.log.debug('Checking cinder:glance image-service relation data...')
#        unit = self.cinder_sentry
#        relation = ['image-service', 'glance:image-service']
#        expected = {'private-address': u.valid_ip}
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('cinder image-service', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_203_glance_cinder_image_service_relation(self):
#        """Verify the glance:cinder image-service relation data"""
#        u.log.debug('Checking glance:cinder image-service relation data...')
#        unit = self.glance_sentry
#        relation = ['image-service', 'cinder:image-service']
#        expected = {
#            'private-address': u.valid_ip,
#            'glance-api-server': u.valid_url
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('glance image-service', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_204_mysql_cinder_db_relation(self):
#        """Verify the mysql:glance shared-db relation data"""
#        u.log.debug('Checking mysql:cinder db relation data...')
#        unit = self.mysql_sentry
#        relation = ['shared-db', 'cinder:shared-db']
#        expected = {
#            'private-address': u.valid_ip,
#            'db_host': u.valid_ip
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('mysql shared-db', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_205_cinder_mysql_db_relation(self):
#        """Verify the cinder:mysql shared-db relation data"""
#        u.log.debug('Checking cinder:mysql db relation data...')
#        unit = self.cinder_sentry
#        relation = ['shared-db', 'mysql:shared-db']
#        expected = {
#            'private-address': u.valid_ip,
#            'hostname': u.valid_ip,
#            'username': 'cinder',
#            'database': 'cinder'
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('cinder shared-db', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_206_keystone_cinder_id_relation(self):
#        """Verify the keystone:cinder identity-service relation data"""
#        u.log.debug('Checking keystone:cinder id relation data...')
#        unit = self.keystone_sentry
#        relation = ['identity-service',
#                    'cinder:identity-service']
#        expected = {
#            'service_protocol': 'http',
#            'service_tenant': 'services',
#            'admin_token': 'ubuntutesting',
#            'service_password': u.not_null,
#            'service_port': '5000',
#            'auth_port': '35357',
#            'auth_protocol': 'http',
#            'private-address': u.valid_ip,
#            'auth_host': u.valid_ip,
#            'service_username': 'cinder_cinderv2',
#            'service_tenant_id': u.not_null,
#            'service_host': u.valid_ip
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('identity-service cinder', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_207_cinder_keystone_id_relation(self):
#        """Verify the cinder:keystone identity-service relation data"""
#        u.log.debug('Checking cinder:keystone id relation data...')
#        unit = self.cinder_sentry
#        relation = ['identity-service',
#                    'keystone:identity-service']
#        expected = {
#            'cinder_service': 'cinder',
#            'cinder_region': 'RegionOne',
#            'cinder_public_url': u.valid_url,
#            'cinder_internal_url': u.valid_url,
#            'cinder_admin_url': u.valid_url,
#            'private-address': u.valid_ip
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('cinder identity-service', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_208_rabbitmq_cinder_amqp_relation(self):
#        """Verify the rabbitmq-server:cinder amqp relation data"""
#        u.log.debug('Checking rmq:cinder amqp relation data...')
#        unit = self.rabbitmq_sentry
#        relation = ['amqp', 'cinder:amqp']
#        expected = {
#            'private-address': u.valid_ip,
#            'password': u.not_null,
#            'hostname': u.valid_ip
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('amqp cinder', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
#    def test_209_cinder_rabbitmq_amqp_relation(self):
#        """Verify the cinder:rabbitmq-server amqp relation data"""
#        u.log.debug('Checking cinder:rmq amqp relation data...')
#        unit = self.cinder_sentry
#        relation = ['amqp', 'rabbitmq-server:amqp']
#        expected = {
#            'private-address': u.valid_ip,
#            'vhost': 'openstack',
#            'username': u.not_null
#        }
#        ret = u.validate_relation_data(unit, relation, expected)
#        if ret:
#            msg = u.relation_error('cinder amqp', ret)
#            amulet.raise_status(amulet.FAIL, msg=msg)
#
    def test_300_cinder_config(self):
        """Verify the data in the cinder.conf file."""
        u.log.debug('Checking cinder config file data...')
        unit = self.cinder_sentry
        conf = '/etc/cinder/cinder.conf'
        unit_mq = self.rabbitmq_sentry
        unit_ks = self.keystone_sentry
        rel_mq_ci = unit_mq.relation('amqp', 'cinder:amqp')
        rel_ks_ci = unit_ks.relation('identity-service',
                                     'cinder:identity-service')
        auth_uri = ('http://%s:%s/' %
                    (rel_ks_ci['auth_host'], rel_ks_ci['service_port']))
        auth_url = ('http://%s:%s/' %
                    (rel_ks_ci['auth_host'], rel_ks_ci['auth_port']))

        expected = {
            'DEFAULT': {
                'use_syslog': 'False',
                'debug': 'False',
                'verbose': 'False',
                'iscsi_helper': 'tgtadm',
                'volume_group': 'cinder-volumes',
                'auth_strategy': 'keystone',
                'volumes_dir': '/var/lib/cinder/volumes'
            },
            'keystone_authtoken': {
                'admin_user': rel_ks_ci['service_username'],
                'admin_password': rel_ks_ci['service_password'],
                'admin_tenant_name': rel_ks_ci['service_tenant'],
                'auth_uri': auth_uri,
                'signing_dir': '/var/cache/cinder'
            }
        }

        expected_rmq = {
            'rabbit_userid': 'cinder',
            'rabbit_virtual_host': 'openstack',
            'rabbit_password': rel_mq_ci['password'],
            'rabbit_host': rel_mq_ci['hostname'],
        }
        if self._get_openstack_release() >= self.trusty_mitaka:
            expected['keystone_authtoken'] = {
                'auth_uri': auth_uri.rstrip('/'),
                'auth_url': auth_url.rstrip('/'),
                'auth_type': 'password',
                'project_domain_name': 'default',
                'user_domain_name': 'default',
                'project_name': 'services',
                'username': rel_ks_ci['service_username'],
                'password': rel_ks_ci['service_password'],
                'signing_dir': '/var/cache/cinder'
            }

        if self._get_openstack_release() == self.trusty_liberty:
            expected['keystone_authtoken'] = {
                'auth_uri': auth_uri.rstrip('/'),
                'auth_url': auth_url.rstrip('/'),
                'auth_plugin': 'password',
                'project_domain_id': 'default',
                'user_domain_id': 'default',
                'project_name': 'services',
                'username': rel_ks_ci['service_username'],
                'password': rel_ks_ci['service_password'],
                'signing_dir': '/var/cache/cinder'
            }

        if self._get_openstack_release() == self.trusty_kilo:
            expected['keystone_authtoken']['auth_uri'] = auth_uri
            expected['keystone_authtoken']['identity_uri'] = \
                auth_url.strip('/')

        if self._get_openstack_release() >= self.trusty_kilo:
            # Kilo or later
            expected['oslo_messaging_rabbit'] = expected_rmq
        else:
            # Juno or earlier
            expected['DEFAULT'].update(expected_rmq)
            expected['keystone_authtoken']['auth_host'] = \
                rel_ks_ci['auth_host']

        for section, pairs in expected.iteritems():
            ret = u.validate_config_data(unit, conf, section, pairs)
            if ret:
                message = "cinder config error: {}".format(ret)
                amulet.raise_status(amulet.FAIL, msg=message)

    def test_301_cinder_logging_config(self):
        """Verify the data in the cinder logging conf file."""
        u.log.debug('Checking cinder logging config file data...')
        unit = self.cinder_sentry
        conf = '/etc/cinder/logging.conf'

        expected = {
            'loggers': {
                'keys': 'root, cinder'
            },
            'logger_cinder': {
                'level': 'INFO',
                'handlers': 'stderr',
                'qualname': 'cinder'
            },
            'logger_root': {
                'level': 'WARNING',
                'handlers': 'null'
            }
        }

        for section, pairs in expected.iteritems():
            ret = u.validate_config_data(unit, conf, section, pairs)
            if ret:
                message = "cinder logging config error: {}".format(ret)
                amulet.raise_status(amulet.FAIL, msg=message)

    def test_303_cinder_rootwrap_config(self):
        """Inspect select config pairs in rootwrap.conf."""
        u.log.debug('Checking cinder rootwrap config file data...')
        unit = self.cinder_sentry
        conf = '/etc/cinder/rootwrap.conf'
        section = 'DEFAULT'
        expected = {
            'filters_path': '/etc/cinder/rootwrap.d,'
                            '/usr/share/cinder/rootwrap',
            'use_syslog': 'False',
        }

        ret = u.validate_config_data(unit, conf, section, expected)
        if ret:
            msg = "cinder rootwrap config error: {}".format(ret)
            amulet.raise_status(amulet.FAIL, msg=msg)

    def test_400_cinder_api_connection(self):
        """Simple api call to check service is up and responding"""
        u.log.debug('Checking basic cinder api functionality...')
        check = list(self.cinder.volumes.list())
        u.log.debug('Cinder api check (volumes.list): {}'.format(check))
        assert(check == [])

    def test_401_create_delete_volume(self):
        """Create a cinder volume and delete it."""
        u.log.debug('Creating, checking and deleting cinder volume...')
        vol_new = u.create_cinder_volume(self.cinder)
        vol_id = vol_new.id
        u.delete_resource(self.cinder.volumes, vol_id, msg="cinder volume")

    def test_402_create_delete_volume_from_image(self):
        """Create a cinder volume from a glance image, and delete it."""
        u.log.debug('Creating, checking and deleting cinder volume'
                    'from glance image...')
        img_new = u.create_cirros_image(self.glance, "cirros-image-1")
        img_id = img_new.id
        vol_new = u.create_cinder_volume(self.cinder,
                                         vol_name="demo-vol-cirros",
                                         img_id=img_id)
        vol_id = vol_new.id
        u.delete_resource(self.glance.images, img_id, msg="glance image")
        u.delete_resource(self.cinder.volumes, vol_id, msg="cinder volume")

    def test_403_volume_snap_clone_extend_inspect(self):
        """Create a cinder volume, clone it, extend its size, create a
        snapshot of the volume, create a volume from a snapshot, check
        status of each, inspect underlying lvm, then delete the resources."""
        u.log.debug('Creating, snapshotting, cloning, extending a '
                    'cinder volume...')
        vols = []

        # Create a 1GB volume
        vol_new = u.create_cinder_volume(self.cinder, vol_size=1)
        vols.append(vol_new)
        vol_id = vol_new.id

        # Snapshot the volume
        snap = self._snapshot_cinder_volume(vol_id=vol_id)
        snap_id = snap.id

        # Create a volume from the snapshot
        vol_from_snap = u.create_cinder_volume(self.cinder,
                                               vol_name="demo-vol-from-snap",
                                               snap_id=snap_id)
        vols.append(vol_from_snap)

        # Clone an existing volume
        vol_clone = u.create_cinder_volume(self.cinder,
                                           vol_name="demo-vol-clone",
                                           src_vol_id=vol_id)
        vols.append(vol_clone)
        vol_clone_id = vol_clone.id

        # Extend the cloned volume and confirm new size
        ret = self._extend_cinder_volume(vol_clone_id, new_size=2)
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

        # Inspect logical volumes (lvm) on cinder unit
        ret = self._check_cinder_lvm()
        if ret:
            amulet.raise_status(amulet.FAIL, msg=ret)

        # Cleanup
        u.log.debug('Deleting snapshot {}...'.format(snap_id))
        u.delete_resource(self.cinder.volume_snapshots,
                          snap_id, msg="cinder volume")

        for vol in vols:
            u.log.debug('Deleting volume {}...'.format(vol.id))
            u.delete_resource(self.cinder.volumes, vol.id, msg="cinder volume")

    def test_900_restart_on_config_change(self):
        """Verify that the specified services are restarted when the
        config is changed."""

        sentry = self.cinder_sentry
        juju_service = 'cinder'

        # Expected default and alternate values
        set_default = {'debug': 'False'}
        set_alternate = {'debug': 'True'}

        # Config file affected by juju set config change
        conf_file = '/etc/cinder/cinder.conf'

        # Services which are expected to restart upon config change
        services = {
            'cinder-api': conf_file,
            'cinder-scheduler': conf_file,
            'cinder-volume': conf_file
        }

        # Make config change, check for service restarts
        u.log.debug('Making config change on {}...'.format(juju_service))
        mtime = u.get_sentry_time(sentry)
        self.d.configure(juju_service, set_alternate)

        sleep_time = 40
        for s, conf_file in services.iteritems():
            u.log.debug("Checking that service restarted: {}".format(s))
            if not u.validate_service_config_changed(sentry, mtime, s,
                                                     conf_file,
                                                     retry_count=4,
                                                     retry_sleep_time=20,
                                                     sleep_time=sleep_time):
                self.d.configure(juju_service, set_default)
                msg = "service {} didn't restart after config change".format(s)
                amulet.raise_status(amulet.FAIL, msg=msg)
            sleep_time = 0

        self.d.configure(juju_service, set_default)

    def test_910_pause_and_resume(self):
        """The services can be paused and resumed. """
        u.log.debug('Checking pause and resume actions...')
        unit_name = "cinder/0"
        unit = self.d.sentry.unit[unit_name]

        assert u.status_get(unit)[0] == "active"

        action_id = self._run_action(unit_name, "pause")
        assert self._wait_on_action(action_id), "Pause action failed."
        assert u.status_get(unit)[0] == "maintenance"

        action_id = self._run_action(unit_name, "resume")
        assert self._wait_on_action(action_id), "Resume action failed."
        assert u.status_get(unit)[0] == "active"
        u.log.debug('OK')

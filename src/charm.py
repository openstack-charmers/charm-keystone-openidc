#!/usr/bin/env python3
#
# Copyright 2022 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import os
import subprocess

from typing import List
from uuid import uuid4

import ops_openstack.core
import requests

from ops.main import main
from ops.model import StatusBase, ActiveStatus, BlockedStatus

from ops_openstack.adapters import (
    ConfigurationAdapter,
)
from charmhelpers.contrib.openstack import templating as os_templating
from charmhelpers.core import host as ch_host
from charmhelpers.core import templating


logger = logging.getLogger(__name__)
CONFIG_DIR = '/etc/apache2/openidc'


class KeystoneOpenIDCError(Exception):
    pass


class KeystoneOpenIDCOptions(ConfigurationAdapter):

    def __init__(self, charm_instance):
        self.charm_instance = charm_instance
        super().__init__(charm_instance)

    def _get_principal_data(self):
        relation = self.charm_instance.model.get_relation(
            'keystone-fid-service-provider')
        if len(relation.units) > 0:
            return relation.data[list(relation.units)[0]]
        else:
            logger.debug('There are no related units via '
                         'keystone-fid-service-provider')
            return None

    @property
    def hostname(self) -> str:
        """Hostname as advertised by the principal charm."""
        data = self._get_principal_data()
        try:
            return json.loads(data['hostname'])
        except (TypeError, KeyError):
            return None

    @property
    def openidc_location_config(self) -> str:
        """Path to the file with the OpenID Connect configuration."""
        return os.path.join(self.charm_instance.config_dir,
                            f'openidc-location.{self.idp_id}.conf')

    @property
    def oidc_auth_path(self) -> str:
        service_name = self.charm_instance.unit.app.name
        return (f'/v3/OS-FEDERATION/identity_providers/{service_name}'
                f'/protocols/openid/auth')

    @property
    def idp_id(self) -> str:
        return 'openid'

    @property
    def scheme(self) -> str:
        data = self._get_principal_data()
        try:
            tls_enabled = json.loads(data['tls-enabled'])
            return 'https' if tls_enabled else 'http'
        except (TypeError, KeyError):
            return None

    @property
    def port(self) -> int:
        data = self._get_principal_data()
        try:
            return json.loads(data['port'])
        except (TypeError, KeyError):
            return None

    @property
    def oidc_crypto_passphrase(self) -> str:

        data = None
        relation = self.charm_instance.model.get_relation('cluster')
        data = relation.data[self.charm_instance.unit.app]

        if not data:
            raise KeystoneOpenIDCError('data bag on peer relation not found')

        crypto_passphrase = data.get('oidc-crypto-passphrase')
        if crypto_passphrase:
            logger.debug('Using oidc-crypto-passphrase from app databag')
            return crypto_passphrase
        else:
            logger.warning('The oidc-crypto-passphrase has not been set')
            return None

    @property
    def metadata(self):
        """Metadata content offered by the Identity Provider.

        The content available at the url configured in
        oidc-provider-metadata-url is read and parsed as json.
        """
        if self.oidc_provider_metadata_url:
            logging.info('GETing content from %s',
                         self.oidc_provider_metadata_url)
            r = requests.get(self.oidc_provider_metadata_url)
            return r.json()
        else:
            logging.info('Metadata was not retrieved since '
                         'oidc-provider-metadata-url is not set')
            return None


class KeystoneOpenIDCCharm(ops_openstack.core.OSBaseCharm):

    PACKAGES = ['libapache2-mod-auth-openidc']

    REQUIRED_RELATIONS = ['keystone-fid-service-provider',
                          'websso-fid-service-provider']

    REQUIRED_KEYS = ['oidc_crypto_passphrase', 'oidc_client_id',
                     'hostname', 'port', 'scheme']
    APACHE2_MODULE = 'auth_openidc'

    CONFIG_FILE_OWNER = 'root'
    CONFIG_FILE_GROUP = 'www-data'

    release = 'xena'  # First release supported.

    auth_method = 'openid'  # the driver to be used.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        super().register_status_check(self._check_status)
        self.options = KeystoneOpenIDCOptions(self)

        # handlers
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.cluster_relation_created,
                               self._on_cluster_relation_created)
        self.framework.observe(self.on.cluster_relation_changed,
                               self._on_cluster_relation_changed)
        # keystone-fid-service-provider
        self.framework.observe(
            self.on.keystone_fid_service_provider_relation_joined,
            self._on_keystone_fid_service_provider_relation_joined
        )
        self.framework.observe(
            self.on.keystone_fid_service_provider_relation_changed,
            self._on_keystone_fid_service_provider_relation_changed
        )

        # websso-fid-service-provider
        self.framework.observe(
            self.on.websso_fid_service_provider_relation_joined,
            self._on_websso_fid_service_provider_relation_joined
        )
        self.framework.observe(
            self.on.websso_fid_service_provider_relation_changed,
            self._on_websso_fid_service_provider_relation_changed
        )

    # Event handlers
    def on_install(self, _):
        """Install hook handler.

        This event handler installs the list of packages defined in the
        property PACKAGES and enables the openidc apache module.
        """
        super().on_install(_)
        self.enable_module()

    def _on_start(self, _):
        """Start hook handler.

        Set the flag `is_started` which is consumed by the update-status
        hook. This charm doesn't run new services, so there is no need to
        start anything.
        """
        self._stored.is_started = True

    def _on_keystone_fid_service_provider_relation_joined(self, event):

        if not self.is_data_ready():
            event.defer()

        self.update_principal_data()

    def update_principal_data(self):
        relation = self.model.get_relation('keystone-fid-service-provider')
        data = relation.data[self.unit]

        # When (if) this patch is merged, we can use auth-method
        # https://review.opendev.org/c/openstack/charm-keystone/+/852601
        # data['auth-method'] = json.dumps(self.auth_method)
        data['protocol-name'] = json.dumps(self.options.idp_id)
        data['remote-id-attribute'] = json.dumps(
            self.options.remote_id_attribute)

    def _on_keystone_fid_service_provider_relation_changed(self, event):
        self.update_config_if_needed()

    def _on_websso_fid_service_provider_relation_joined(self, event):
        pass

    def _on_websso_fid_service_provider_relation_changed(self, event):
        pass

    def _on_config_changed(self, event):
        self._stored.is_started = True
        if not self.is_data_ready():
            logger.debug(f'relation data is not ready yet, deferring {event}')
            event.defer()
            return

        self.update_config_if_needed()
        self.update_principal_data()

    def update_config_if_needed(self):
        with ch_host.restart_on_change(
                self.restart_map,
                restart_functions=self.restart_functions):
            self.render_config()

    def _on_cluster_relation_created(self, _):

        if self.unit.is_leader():
            # we need to set the client secret since we are the leader and the
            # secret hasn't been set.
            data = None
            relations = self.framework.model.relations.get(
                'cluster')
            for relation in relations:
                data = relation.data[self.unit.app]
                break
            logger.info('Generating oidc-crypto-passphrase')
            data.update({'oidc-crypto-passphrase': str(uuid4())})
        else:
            logger.debug('Not leader, skipping oidc-crypto-passphrase '
                         'generation')

    def _on_cluster_relation_changed(self, _):
        self._on_config_changed(_)

    def is_data_ready(self) -> bool:
        if not self.model.get_relation('cluster'):
            return False

        return len(self.find_missing_keys()) == 0

    def find_missing_keys(self) -> List[str]:

        """Find keys not set that are needed for the charm to work correctly.

        :returns: List of configuration keys that need to be set and are not.
        """
        options = KeystoneOpenIDCOptions(self)
        missing_keys = []
        for key in self.REQUIRED_KEYS:
            if getattr(options, key) in [None, '']:
                missing_keys.append(key)

        if missing_keys:
            logger.debug('Incomplete data: %s', ' '.join(missing_keys))

        return missing_keys

    def services(self) -> List[str]:
        """Determine the list of services that should be running."""
        return []

    def _check_status(self) -> StatusBase:
        if self.is_data_ready():
            return ActiveStatus()
        else:
            return BlockedStatus('incomplete data')

    def enable_module(self):
        """Enable oidc Apache module."""
        logger.info('Enabling apache2 module: %s', self.APACHE2_MODULE)
        subprocess.check_call(['a2enmod', self.APACHE2_MODULE])

    def disable_module(self):
        """Disable oidc Apache module."""
        logger.info('Disabling apache2 module: %s', self.APACHE2_MODULE)
        subprocess.check_call(['a2dismod', self.APACHE2_MODULE])

    def request_restart(self, service_name=None):
        """Request a restart of the service to the principal.

        :param service_name: name of the service to restart, but unused.
        """
        relation = self.model.get_relation('keystone-fid-service-provider')
        data = relation.data[self.unit]

        logger.info('Requesting a restart to the principal charm')
        data['restart-nonce'] = json.dumps(str(uuid4()))

    def render_config(self):
        """Render Service Provider configuration files to be used by Apache."""
        ch_host.mkdir(self.config_dir,
                      perms=0o750,
                      owner=self.CONFIG_FILE_OWNER,
                      group=self.CONFIG_FILE_GROUP)
        templating.render(
            source='apache-openidc-location.conf',
            template_loader=os_templating.get_loader('templates/',
                                                     self.release),
            target=self.options.openidc_location_config,
            context={'options': KeystoneOpenIDCOptions(self)},
            owner=self.CONFIG_FILE_OWNER,
            group=self.CONFIG_FILE_GROUP,
            perms=0o440
        )

    # properties
    @property
    def restart_map(self):
        return {self.options.openidc_location_config: ['apache2']}

    @property
    def restart_functions(self):
        return {'apache2': self.request_restart}

    @property
    def config_dir(self):
        return CONFIG_DIR


if __name__ == "__main__":
    main(KeystoneOpenIDCCharm)

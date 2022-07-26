import logging
import os
import sys
import tempfile
import unittest
import uuid

from unittest import mock

import requests_mock
from ops.testing import Harness

sys.path.append('src')  # noqa

import charm


logger = logging.getLogger(__name__)
WELL_KNOWN_URL = 'https://example.com/.well-known/openid-configuration'
WELL_KNOWN_URL_INVALID = 'http://example.com/.well-known/openid-configuration'
INTROSPECTION_ENDPOINT_INVALID = 'http://idp.example.com/oauth2'
CRYPTO_PASSPHRASE = '1e19bb8a-a92d-4377-8226-5e8fc475822c'


class BaseTestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(charm.KeystoneOpenIDCCharm, meta='''
            name: keystone-openidc
            provides:
              keystone-fid-service-provider:
                interface: keystone-fid-service-provider
                scope: container
              websso-fid-service-provider:
                interface: websso-fid-service-provider
                scope: global
            peers:
              cluster:
                interface: cluster
        ''')
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()


class TestRelations(BaseTestCharm):
    def test_add_relation(self):
        self.harness.add_relation('keystone-fid-service-provider', 'keystone')


class TestCharm(BaseTestCharm):
    def setUp(self):
        super().setUp()

        # bootstrap the charm
        self.crypto_passphrase = uuid.UUID(CRYPTO_PASSPHRASE)

        # disable hooks to avoid trigger them implicitly while the relations
        # are being setup and the mocks are not in place yet.
        self.harness.disable_hooks()

        # configure relation keystone <-> keystone-openidc
        rid = self.harness.add_relation('keystone-fid-service-provider',
                                        'keystone')
        self.harness.add_relation_unit(rid, 'keystone/0')
        self.harness.update_relation_data(rid, 'keystone/0',
                                          {'port': '5000',
                                           'tls-enabled': 'true',
                                           'hostname': '"10.5.250.250"'})

        # configure peer relation for keystone-openidc
        logger.debug(f'Adding cluster relation for '
                     f'{self.harness.charm.unit.app.name}')
        rid = self.harness.add_relation('cluster',
                                        self.harness.charm.unit.app.name)
        self.harness.update_relation_data(
            rid, self.harness.charm.unit.app.name,
            {'oidc-crypto-passphrase': str(self.crypto_passphrase)})

    @mock.patch('os.fchown')
    @mock.patch('os.chown')
    def test_render_config_leader(self, chown, fchown):
        self.harness.set_leader(True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("charm.KeystoneOpenIDCCharm.config_dir",
                            new_callable=mock.PropertyMock,
                            return_value=tmpdir):
                self.harness.update_config(
                    key_values={'oidc-provider-metadata-url': WELL_KNOWN_URL})
                self.harness.charm.render_config()
                fpath = self.harness.charm.options.openidc_location_config
                self.assertTrue(os.path.isfile(fpath))
                with open(fpath) as f:
                    content = f.read()
                    self.assertIn(f'OIDCProviderMetadataURL {WELL_KNOWN_URL}',
                                  content)
                    self.assertIn(
                        f'OIDCCryptoPassphrase {str(self.crypto_passphrase)}',
                        content
                    )

    def test_find_missing_keys_no_metadata_url(self):
        opts = {
            'oidc-provider-metadata-url': '',
        }
        self.harness.update_config(key_values=opts)
        missing_keys = self.harness.charm.find_missing_keys()
        missing_keys.sort()

        expected = ['oidc_client_id', 'oidc_provider_metadata_url']
        expected.sort()
        self.assertEqual(missing_keys, expected)

    def test_find_missing_keys_manual_configuration(self):
        opts = {
            'oidc-provider-metadata-url': '',
            'oidc-provider-issuer': 'foo',
            'oidc-client-id': 'keystone',
        }
        self.harness.update_config(key_values=opts)
        missing_keys = self.harness.charm.find_missing_keys()
        missing_keys.sort()

        expected = ['oidc_provider_auth_endpoint',
                    'oidc_provider_token_endpoint',
                    'oidc_provider_token_endpoint_auth',
                    'oidc_provider_user_info_endpoint',
                    'oidc_provider_jwks_uri']
        expected.sort()
        self.assertEqual(missing_keys, expected)

    def test_find_missing_keys_invalid_oidc_oauth_verify_jwks_uri(self):
        opts = {
            'oidc-provider-metadata-url': WELL_KNOWN_URL,
            'oidc-provider-issuer': 'foo',
            'oidc-client-id': 'keystone',
            'oidc-oauth-verify-jwks-uri': 'http://idp.example.com/jwks'
        }

        self.harness.update_config(key_values=opts)
        self.assertRaises(charm.CharmConfigError,
                          self.harness.charm.find_missing_keys)

    def test_find_missing_keys_invalid_introspection_endpoint(self):
        opts = {
            'oidc-provider-metadata-url': WELL_KNOWN_URL,
            'oidc-provider-issuer': 'foo',
            'oidc-client-id': 'keystone',
            'oidc-oauth-verify-jwks-uri': 'http://idp.example.com/jwks'
        }

        well_known_url_content = {
            'introspection_endpoint': INTROSPECTION_ENDPOINT_INVALID,
        }
        self.harness.update_config(key_values=opts)
        with requests_mock.Mocker() as m:
            m.get(WELL_KNOWN_URL, json=well_known_url_content)
            self.assertRaises(charm.CharmConfigError,
                              self.harness.charm.find_missing_keys)

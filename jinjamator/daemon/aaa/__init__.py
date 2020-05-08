from flask import g, url_for
from authlib.integrations.flask_client import OAuth, OAuthError
from jinjamator.task.configuration import TaskConfiguration
from jinjamator.plugin_loader.content import init_loader
from jinjamator.daemon.app import app
from jinjamator.daemon.aaa.models import User
from jinjamator.daemon.database import db

import random
from pprint import pformat
from glob import glob
import os
from copy import deepcopy
import logging
import string

log = logging.getLogger()
aaa_providers = {}
_global_ldr = init_loader(None)


class AuthProviderBase(object):
    def __init__(self, app=None):
        self._app = app
        self._log = logging.getLogger()
        self._user = None
        self._redirect_uri = None 

    
    def register(self,**kwargs):
        self._log.info('not implemented')

    def init_app(self,app):
        self._app = app

    def login(self, request):
        self._log.info('not implemented')

    def authorize(self, request):
        self._log.info('not implemented')

    @property
    def logged_in_user(self):
        return self._user.username
    

class LocalAuthProvider(AuthProviderBase):
    def __init__(self, app=None):
        super(LocalAuthProvider, self).__init__(app)
        self._name = 'local'

    def login(self, request):
        username = request.json.get('username')
        password = request.json.get('password')
        self._user = self.verify_password(username, password)
        if self._user:
            return True
        return False
 
    def verify_password(self, username_or_token, password):
        # first try to authenticate by token
        user = User.verify_auth_token(username_or_token)
        if not user:
            # try to authenticate with username/password
            user = User.query.filter_by(username=username_or_token).first()
            if not user or not user.verify_password(password):
                return None
        return user


class AuthLibAuthProvider(AuthProviderBase):
    def __init__(self, app=None):
        super(AuthLibAuthProvider, self).__init__(app)
        if self._app:
            self._oauth = OAuth(self._app)
        else:
            self._oauth = OAuth()
        self._name = None
        self._provider = None

    def init_app(self, app):
        """
        Init oauth instance within Flask
        """
        self._app = app
        self._oauth.init_app(app)


    def register(self,**kwargs):
        """
        Register openid connect provider
        """

        self._name = kwargs.get('name')
        self._oauth.register(**kwargs)

    def login(self, request):
        """
        Start openid connect login procedure
        """
        if self._redirect_uri:
            redirect_uri = self._redirect_uri
        else:
            redirect_uri = url_for('api.aaa_auth', _external=True)
            self._redirect_uri = redirect_uri
        
        self._log.debug(f'Start openid connect login procedure via oidc provider {self._name}')
        self._provider = getattr(self._oauth, self._name)
        return self._provider.authorize_redirect(redirect_uri)

    def authorize(self, request):
        if self._provider:
            self._access_token = self._provider.authorize_access_token()
            self._id_token = self._provider.parse_id_token(self._access_token)
            self._log.debug(f'got access token from {self._name}')
            self._log.debug(pformat(self._access_token))
            self._log.debug(f'got userdata from {self._name}')
            self._log.debug(pformat(self._id_token))
            self._username = self._id_token['preferred_username']
            user = User.query.filter_by(username=self._username).first()
            if user is None:
                self._log.info(f'username {self._username} not found in local database -> creating')
                user = User(username=self._username)                
                user.name = self._id_token['name']
                
            else:
                self._log.info(f'username {self._username} found in local database')
            user.aaa_provider = self._name
            user.hash_password(''.join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(128))) # generate max len random secure password on each login
            db.session.add(user)
            db.session.commit()
            self._user=user
            return True
        else:
            self._log.warn(f'AAA provider {self._name} has not been initialized properly')
        return False
        



def initialize(aaa_providers, _configuration):

    for content_plugin_dir in _configuration.get(
        "global_content_plugins_base_dirs", []
    ):
        _global_ldr.load(f"{content_plugin_dir}")

    for aaa_config_directory in _configuration.get('aaa_configuration_base_dirs',[]):
        for config_file in glob(os.path.join(aaa_config_directory,'*.yaml')):
            cur_cfg = TaskConfiguration()
            cur_cfg.merge_yaml(config_file)
            prov_name = cur_cfg.get('authlib_configuration', {}).get('name')
            if cur_cfg.get('type') == 'authlib':
                app.config[f'{prov_name.upper()}_CLIENT_ID'] = cur_cfg.get('client_id')
                app.config[f'{prov_name.upper()}_CLIENT_SECRET'] = cur_cfg.get('client_secret')
                if prov_name not in aaa_providers:
                    aaa_providers[prov_name] = AuthLibAuthProvider()
                aaa_providers[prov_name].register(
                    **deepcopy(cur_cfg.get('authlib_configuration', {}))
                )
                aaa_providers[prov_name].init_app(app)
                if cur_cfg.get('redirect_uri'):
                    aaa_providers[prov_name]._redirect_uri = cur_cfg.get('redirect_uri')
            elif cur_cfg.get('type') == 'local':
                prov_name = cur_cfg.get('name')
                log.debug(pformat(cur_cfg._data))
                if cur_cfg.get('redirect_uri'):
                    aaa_providers[prov_name]._redirect_uri = cur_cfg.get('redirect_uri')
                aaa_providers[prov_name] = LocalAuthProvider()



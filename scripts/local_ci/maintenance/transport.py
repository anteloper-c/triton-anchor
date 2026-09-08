"""Host-only Git transport configuration, shared by result and health publishers."""
import base64
import os
from pathlib import Path
from urllib.parse import urlparse


def validate_relay_url(url):
    if not isinstance(url, str) or not url or 'race-org/triton-anchor' in url.lower():
        raise ValueError('relay repository is not authorized')
    if Path(url).is_absolute():
        if not Path(url).resolve().is_dir():
            raise ValueError('local relay does not exist')
        return
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('relay must be an explicit HTTPS URL without credentials or an absolute local Git path')


def git_environment(relay):
    """Return child env; tokens never enter argv, git config files or errors.

    Header matching is limited to the configured relay URL. Global/system Git
    config and credential helpers cannot silently select a different account.
    """
    url = relay['url']
    validate_relay_url(url)
    env = os.environ.copy()
    # Caller-shell repository overrides or Git tracing must not redirect a host
    # operation or write the authorization header into diagnostic files.
    for key in list(env):
        if key.startswith('GIT_'):
            env.pop(key)
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='')
    token = os.environ.get(relay.get('token_env', 'GITEE_TOKEN'), '')
    username = os.environ.get(relay.get('username_env', 'GITEE_USERNAME'), '')
    if url.startswith('https://') and token:
        if not username:
            raise ValueError('configured relay username environment variable is missing')
        authorization = base64.b64encode((username + ':' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_1='http.' + url + '.extraheader',
                   GIT_CONFIG_VALUE_1='Authorization: Basic ' + authorization)
    return env

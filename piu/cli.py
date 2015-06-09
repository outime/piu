#!/usr/bin/env python3
'''
Helper script to request access to a certain host.
'''

import click
import ipaddress
import json
import keyring
import os
import requests
import socket
import yaml
import zign.api


from clickclick import error, info

import piu

try:
    import pyperclip
except:
    pyperclip = None


KEYRING_KEY = 'piu'

CONFIG_DIR_PATH = click.get_app_dir('piu')
CONFIG_FILE_PATH = os.path.join(CONFIG_DIR_PATH, 'piu.yaml')

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

STUPS_CIDR = ipaddress.ip_network('172.31.0.0/16')


def load_config(path):
    if os.path.exists(path):
        with open(path, 'rb') as fd:
            config = yaml.safe_load(fd)
        if not isinstance(config, dict):
            config = {}
    else:
        config = {}
    return config


def store_config(config, path):
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    with open(path, 'w') as fd:
        yaml.dump(config, fd)


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('Piu {}'.format(piu.__version__))
    ctx.exit()


def request_access(even_url, cacert, username, hostname, reason, remote_host, lifetime, user, password, clip):
    data = {'username': username, 'hostname': hostname, 'reason': reason}
    host_via = hostname
    if remote_host:
        data['remote_host'] = remote_host
        host_via = '{} via {}'.format(remote_host, hostname)
    if lifetime:
        data['lifetime_minutes'] = lifetime
    token = zign.api.get_named_token(['uid'], 'employees', 'piu', user, password)
    access_token = token.get('access_token')
    click.secho('Requesting access to host {host_via} for {username}..'.format(host_via=host_via, username=username),
                bold=True)
    r = requests.post(even_url, headers={'Content-Type': 'application/json',
                                         'Authorization': 'Bearer {}'.format(access_token)},
                      data=json.dumps(data),
                      verify=cacert)
    if r.status_code == 200:
        click.secho(r.text, fg='green', bold=True)
        ssh_command = ''
        if remote_host:
            ssh_command = 'ssh -o StrictHostKeyChecking=no {username}@{remote_host}'.format(**vars())
        click.secho('You can now access your server with the following command:')
        command = 'ssh -tA {username}@{hostname} {ssh_command}'.format(
                  username=username, hostname=hostname, ssh_command=ssh_command)
        click.secho(command)
        if clip:
            click.secho('\nOr just check your clipboard and run ctrl/command + v (requires package "xclip" on Linux)')
            if pyperclip is not None:
                pyperclip.copy(command)
    else:
        click.secho('Server returned status {code}: {text}'.format(code=r.status_code, text=r.text),
                    fg='red', bold=True)
    return r.status_code


@click.command(context_settings=CONTEXT_SETTINGS)
@click.argument('host', metavar='[USER]@HOST')
@click.argument('reason')
@click.argument('reason_cont', nargs=-1, metavar='[..]')
@click.option('-u', '--user', help='Username to use for authentication', envvar='USER', metavar='NAME')
@click.option('-p', '--password', help='Password to use for authentication', envvar='PIU_PASSWORD', metavar='PWD')
@click.option('-E', '--even-url', help='Even SSH Access Granting Service URL', envvar='EVEN_URL', metavar='URI')
@click.option('-O', '--odd-host', help='Odd SSH bastion hostname', envvar='ODD_HOST', metavar='HOSTNAME')
@click.option('-t', '--lifetime', help='Lifetime of the SSH access request in minutes (default: 60)',
              type=click.IntRange(1, 525600, clamp=True))
@click.option('--insecure', help='Do not verify SSL certificate', is_flag=True, default=False)
@click.option('--config-file', '-c', help='Use alternative configuration file',
              default=CONFIG_FILE_PATH, metavar='PATH')
@click.option('-V', '--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Print the current version number and exit.')
@click.option('--clip', is_flag=True, help='Copy SSH command into clipboard', default=False)
def cli(host, user, password, even_url, odd_host, reason, reason_cont, insecure, config_file, lifetime, clip):
    '''Request SSH access to a single host'''

    parts = host.split('@')
    if len(parts) > 1:
        username = parts[0]
    else:
        username = user

    hostname = parts[-1]

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    reason = ' '.join([reason] + list(reason_cont)).strip()

    cacert = not insecure

    config = load_config(config_file)

    even_url = even_url or config.get('even_url')
    odd_host = odd_host or config.get('odd_host')
    if 'cacert' in config:
        cacert = config['cacert']

    while not even_url:
        even_url = click.prompt('Please enter the Even SSH access granting service URL')
        if not even_url.startswith('http'):
            # convenience for humans: add HTTPS by default
            even_url = 'https://{}'.format(even_url)
        try:
            requests.get(even_url)
        except:
            error('Could not reach {}'.format(even_url))
            even_url = None
        config['even_url'] = even_url

    while ip and ip in STUPS_CIDR and not odd_host:
        odd_host = click.prompt('Please enter the Odd SSH bastion hostname')
        try:
            socket.getaddrinfo(odd_host, 22)
        except:
            error('Could not resolve hostname {}'.format(odd_host))
            odd_host = None
        config['odd_host'] = odd_host

    store_config(config, config_file)

    password = password or keyring.get_password(KEYRING_KEY, user)

    if not password:
        password = click.prompt('Password', hide_input=True)

    if not even_url.endswith('/access-requests'):
        even_url = even_url.rstrip('/') + '/access-requests'

    first_host = hostname
    remote_host = hostname
    if odd_host:
        first_host = odd_host

    if first_host == remote_host:
        # user friendly behavior: it makes no sense to jump from bastion to itself
        remote_host = None
    elif remote_host.startswith('odd-'):
        # user friendly behavior: if the remote host is obviously a odd host, just use it
        first_host = remote_host
        remote_host = None

    return_code = request_access(even_url, cacert, username, first_host, reason, remote_host, lifetime,
                                 user, password, clip)

    if return_code == 200:
        keyring.set_password(KEYRING_KEY, user, password)
    elif return_code == 403:
        info('Please check your username and password and try again.')
        # delete the "wrong" password from the keyring to get a prompt next time
        keyring.set_password(KEYRING_KEY, user, '')


def main():
    cli()

if __name__ == '__main__':
    main()

#!/usr/bin/python
import subprocess
import os
import urlparse
import time

from base64 import b64encode
from collections import OrderedDict
from copy import deepcopy

from charmhelpers.contrib.hahelpers.cluster import(
    eligible_leader,
    determine_api_port,
    https,
    is_clustered
    )

from charmhelpers.contrib.openstack import context, templating

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    error_out,
    get_os_codename_install_source,
    os_release,
    save_script_rc as _save_script_rc)

import charmhelpers.contrib.unison as unison

from charmhelpers.core.hookenv import (
    config,
    log,
    relation_get,
    relation_set,
    unit_private_ip,
    INFO,
)

from charmhelpers.fetch import (
    apt_install,
    apt_update,
)

from charmhelpers.core.host import (
    service_stop,
    service_start,
)

import keystone_context
import keystone_ssl as ssl

TEMPLATES = 'templates/'

# removed from original: charm-helper-sh
BASE_PACKAGES = [
    'apache2',
    'haproxy',
    'openssl',
    'python-keystoneclient',
    'python-mysqldb',
    'pwgen',
    'unison',
    'uuid',
]

BASE_SERVICES = [
    'keystone',
]

API_PORTS = {
    'keystone-admin': config('admin-port'),
    'keystone-public': config('service-port')
}

KEYSTONE_CONF = "/etc/keystone/keystone.conf"
STORED_PASSWD = "/var/lib/keystone/keystone.passwd"
STORED_TOKEN = "/var/lib/keystone/keystone.token"
SERVICE_PASSWD_PATH = '/var/lib/keystone/services.passwd'

HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
APACHE_CONF = '/etc/apache2/sites-available/openstack_https_frontend'
APACHE_24_CONF = '/etc/apache2/sites-available/openstack_https_frontend.conf'

SSL_DIR = '/var/lib/keystone/juju_ssl/'
SSL_CA_NAME = 'Ubuntu Cloud'
CLUSTER_RES = 'res_ks_vip'
SSH_USER = 'juju_keystone'

BASE_RESOURCE_MAP = OrderedDict([
    (KEYSTONE_CONF, {
        'services': BASE_SERVICES,
        'contexts': [keystone_context.KeystoneContext(),
                     context.SharedDBContext(),
                     context.SyslogContext(),
                     keystone_context.HAProxyContext()],
    }),
    (HAPROXY_CONF, {
        'contexts': [context.HAProxyContext(),
                     keystone_context.HAProxyContext()],
        'services': ['haproxy'],
    }),
    (APACHE_CONF, {
        'contexts': [keystone_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (APACHE_24_CONF, {
        'contexts': [keystone_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
])

CA_CERT_PATH = '/usr/local/share/ca-certificates/keystone_juju_ca_cert.crt'

valid_services = {
    "nova": {
        "type": "compute",
        "desc": "Nova Compute Service"
    },
    "nova-volume": {
        "type": "volume",
        "desc": "Nova Volume Service"
    },
    "cinder": {
        "type": "volume",
        "desc": "Cinder Volume Service"
    },
    "ec2": {
        "type": "ec2",
        "desc": "EC2 Compatibility Layer"
    },
    "glance": {
        "type": "image",
        "desc": "Glance Image Service"
    },
    "s3": {
        "type": "s3",
        "desc": "S3 Compatible object-store"
    },
    "swift": {
        "type": "object-store",
        "desc": "Swift Object Storage Service"
    },
    "quantum": {
        "type": "network",
        "desc": "Quantum Networking Service"
    },
    "oxygen": {
        "type": "oxygen",
        "desc": "Oxygen Cloud Image Service"
    },
    "ceilometer": {
        "type": "metering",
        "desc": "Ceilometer Metering Service"
    },
    "heat": {
        "type": "orchestration",
        "desc": "Heat Orchestration API"
    },
    "heat-cfn": {
        "type": "cloudformation",
        "desc": "Heat CloudFormation API"
    }
}


def resource_map():
    '''
    Dynamically generate a map of resources that will be managed for a single
    hook execution.
    '''
    resource_map = deepcopy(BASE_RESOURCE_MAP)

    if os.path.exists('/etc/apache2/conf-available'):
        resource_map.pop(APACHE_CONF)
    else:
        resource_map.pop(APACHE_24_CONF)
    return resource_map


def register_configs():
    release = os_release('keystone')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def restart_map():
    return OrderedDict([(cfg, v['services'])
                        for cfg, v in resource_map().iteritems()
                        if v['services']])


def determine_ports():
    '''Assemble a list of API ports for services we are managing'''
    ports = [config('admin-port'), config('service-port')]
    return list(set(ports))


def api_port(service):
    return API_PORTS[service]


def determine_packages():
    # currently all packages match service names
    packages = [] + BASE_PACKAGES
    for k, v in resource_map().iteritems():
        packages.extend(v['services'])
    return list(set(packages))


def save_script_rc():
    env_vars = {'OPENSTACK_SERVICE_KEYSTONE': 'keystone',
                'OPENSTACK_PORT_ADMIN': determine_api_port(
                    api_port('keystone-admin')),
                'OPENSTACK_PORT_PUBLIC': determine_api_port(
                    api_port('keystone-public'))}
    _save_script_rc(**env_vars)


def do_openstack_upgrade(configs):
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)
    log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    apt_update()

    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]

    apt_install(packages=determine_packages(), options=dpkg_opts, fatal=True)

    # set CONFIGS to load templates from new release and regenerate config
    configs.set_release(openstack_release=new_os_rel)
    configs.write_all()

    if eligible_leader(CLUSTER_RES):
        migrate_database()


def migrate_database():
    '''Runs keystone-manage to initialize a new database or migrate existing'''
    log('Migrating the keystone database.', level=INFO)
    service_stop('keystone')
    cmd = ['keystone-manage', 'db_sync']
    subprocess.check_output(cmd)
    service_start('keystone')
    time.sleep(10)


## OLD

def get_local_endpoint():
    """ Returns the URL for the local end-point bypassing haproxy/ssl """
    local_endpoint = 'http://localhost:{}/v2.0/'.format(
        determine_api_port(api_port('keystone-admin'))
        )
    return local_endpoint


def set_admin_token(admin_token='None'):
    """Set admin token according to deployment config or use a randomly
       generated token if none is specified (default).
    """
    if admin_token != 'None':
        log('Configuring Keystone to use a pre-configured admin token.')
        token = admin_token
    else:
        log('Configuring Keystone to use a random admin token.')
        if os.path.isfile(STORED_TOKEN):
            msg = 'Loading a previously generated' \
                  ' admin token from %s' % STORED_TOKEN
            log(msg)
            f = open(STORED_TOKEN, 'r')
            token = f.read().strip()
            f.close()
        else:
            cmd = ['pwgen', '-c', '32', '1']
            token = str(subprocess.check_output(cmd)).strip()
            out = open(STORED_TOKEN, 'w')
            out.write('%s\n' % token)
            out.close()
    return(token)


def get_admin_token():
    """Temporary utility to grab the admin token as configured in
       keystone.conf
    """
    with open(KEYSTONE_CONF, 'r') as f:
        for l in f.readlines():
            if l.split(' ')[0] == 'admin_token':
                try:
                    return l.split('=')[1].strip()
                except:
                    error_out('Could not parse admin_token line from %s' %
                              KEYSTONE_CONF)
    error_out('Could not find admin_token line in %s' % KEYSTONE_CONF)


def create_service_entry(service_name, service_type, service_desc, owner=None):
    """ Add a new service entry to keystone if one does not already exist """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    for service in [s._info for s in manager.api.services.list()]:
        if service['name'] == service_name:
            log("Service entry for '%s' already exists." % service_name)
            return
    manager.api.services.create(name=service_name,
                                service_type=service_type,
                                description=service_desc)
    log("Created new service entry '%s'" % service_name)


def create_endpoint_template(region, service,  publicurl, adminurl,
                             internalurl):
    """ Create a new endpoint template for service if one does not already
        exist matching name *and* region """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    service_id = manager.resolve_service_id(service)
    for ep in [e._info for e in manager.api.endpoints.list()]:
        if ep['service_id'] == service_id and ep['region'] == region:
            log("Endpoint template already exists for '%s' in '%s'"
                  % (service, region))

            up_to_date = True
            for k in ['publicurl', 'adminurl', 'internalurl']:
                if ep[k] != locals()[k]:
                    up_to_date = False

            if up_to_date:
                return
            else:
                # delete endpoint and recreate if endpoint urls need updating.
                log("Updating endpoint template with new endpoint urls.")
                manager.api.endpoints.delete(ep['id'])

    manager.api.endpoints.create(region=region,
                                 service_id=service_id,
                                 publicurl=publicurl,
                                 adminurl=adminurl,
                                 internalurl=internalurl)
    log("Created new endpoint template for '%s' in '%s'" % (region, service))


def create_tenant(name):
    """ creates a tenant if it does not already exist """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    tenants = [t._info for t in manager.api.tenants.list()]
    if not tenants or name not in [t['name'] for t in tenants]:
        manager.api.tenants.create(tenant_name=name,
                                   description='Created by Juju')
        log("Created new tenant: %s" % name)
        return
    log("Tenant '%s' already exists." % name)


def create_user(name, password, tenant):
    """ creates a user if it doesn't already exist, as a member of tenant """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    users = [u._info for u in manager.api.users.list()]
    if not users or name not in [u['name'] for u in users]:
        tenant_id = manager.resolve_tenant_id(tenant)
        if not tenant_id:
            error_out('Could not resolve tenant_id for tenant %s' % tenant)
        manager.api.users.create(name=name,
                                 password=password,
                                 email='juju@localhost',
                                 tenant_id=tenant_id)
        log("Created new user '%s' tenant: %s" % (name, tenant_id))
        return
    log("A user named '%s' already exists" % name)


def create_role(name, user=None, tenant=None):
    """ creates a role if it doesn't already exist. grants role to user """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    roles = [r._info for r in manager.api.roles.list()]
    if not roles or name not in [r['name'] for r in roles]:
        manager.api.roles.create(name=name)
        log("Created new role '%s'" % name)
    else:
        log("A role named '%s' already exists" % name)

    if not user and not tenant:
        return

    # NOTE(adam_g): Keystone client requires id's for add_user_role, not names
    user_id = manager.resolve_user_id(user)
    role_id = manager.resolve_role_id(name)
    tenant_id = manager.resolve_tenant_id(tenant)

    if None in [user_id, role_id, tenant_id]:
        error_out("Could not resolve [%s, %s, %s]" %
                   (user_id, role_id, tenant_id))

    grant_role(user, name, tenant)


def grant_role(user, role, tenant):
    """grant user+tenant a specific role"""
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    log("Granting user '%s' role '%s' on tenant '%s'" % \
       (user, role, tenant))
    user_id = manager.resolve_user_id(user)
    role_id = manager.resolve_role_id(role)
    tenant_id = manager.resolve_tenant_id(tenant)

    cur_roles = manager.api.roles.roles_for_user(user_id, tenant_id)
    if not cur_roles or role_id not in [r.id for r in cur_roles]:
        manager.api.roles.add_user_role(user=user_id,
                                        role=role_id,
                                        tenant=tenant_id)
        log("Granted user '%s' role '%s' on tenant '%s'" % \
           (user, role, tenant))
    else:
        log("User '%s' already has role '%s' on tenant '%s'" % \
           (user, role, tenant))


def ensure_initial_admin(config):
    """ Ensures the minimum admin stuff exists in whatever database we're
        using.
        This and the helper functions it calls are meant to be idempotent and
        run during install as well as during db-changed.  This will maintain
        the admin tenant, user, role, service entry and endpoint across every
        datastore we might use.
        TODO: Possibly migrate data from one backend to another after it
        changes?
    """
    create_tenant("admin")
    create_tenant(config("service-tenant"))

    passwd = ""
    if config("admin-password") != "None":
        passwd = config("admin-password")
    elif os.path.isfile(STORED_PASSWD):
        log("Loading stored passwd from %s" % STORED_PASSWD)
        passwd = open(STORED_PASSWD, 'r').readline().strip('\n')
    if passwd == "":
        log("Generating new passwd for user: %s" % \
                  config("admin-user"))
        cmd = ['pwgen', '-c', '16', '1']
        passwd = str(subprocess.check_output(cmd)).strip()
        open(STORED_PASSWD, 'w+').writelines("%s\n" % passwd)

    create_user(config('admin-user'), passwd, tenant='admin')
    update_user_password(config('admin-user'), passwd)
    create_role(config('admin-role'), config('admin-user'), 'admin')
    # TODO(adam_g): The following roles are likely not needed since redux merge
    create_role("KeystoneAdmin", config("admin-user"), 'admin')
    create_role("KeystoneServiceAdmin", config("admin-user"), 'admin')
    create_service_entry("keystone", "identity", "Keystone Identity Service")

    if is_clustered():
        log("Creating endpoint for clustered configuration")
        service_host = auth_host = config("vip")
    else:
        log("Creating standard endpoint")
        service_host = auth_host = unit_private_ip()

    for region in config('region').split():
        create_keystone_endpoint(service_host=service_host,
                                 service_port=config("service-port"),
                                 auth_host=auth_host,
                                 auth_port=config("admin-port"),
                                 region=region)


def create_keystone_endpoint(service_host, service_port,
                             auth_host, auth_port, region):
    proto = 'http'
    if https():
        log("Setting https keystone endpoint")
        proto = 'https'
    public_url = "%s://%s:%s/v2.0" % (proto, service_host, service_port)
    admin_url = "%s://%s:%s/v2.0" % (proto, auth_host, auth_port)
    internal_url = "%s://%s:%s/v2.0" % (proto, service_host, service_port)
    create_endpoint_template(region, "keystone", public_url,
                             admin_url, internal_url)


def update_user_password(username, password):
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    log("Updating password for user '%s'" % username)

    user_id = manager.resolve_user_id(username)
    if user_id is None:
        error_out("Could not resolve user id for '%s'" % username)

    manager.api.users.update_password(user=user_id, password=password)
    log("Successfully updated password for user '%s'" % \
              username)


def load_stored_passwords(path=SERVICE_PASSWD_PATH):
    creds = {}
    if not os.path.isfile(path):
        return creds

    stored_passwd = open(path, 'r')
    for l in stored_passwd.readlines():
        user, passwd = l.strip().split(':')
        creds[user] = passwd
    return creds


def save_stored_passwords(path=SERVICE_PASSWD_PATH, **creds):
    with open(path, 'wb') as stored_passwd:
        [stored_passwd.write('%s:%s\n' % (u, p)) for u, p in creds.iteritems()]


def get_service_password(service_username):
    creds = load_stored_passwords()
    if service_username in creds:
        return creds[service_username]

    passwd = subprocess.check_output(['pwgen', '-c', '32', '1']).strip()
    creds[service_username] = passwd
    save_stored_passwords(**creds)

    return passwd


def synchronize_service_credentials():
    '''
    Broadcast service credentials to peers or consume those that have been
    broadcasted by peer, depending on hook context.
    '''
    if (not eligible_leader(CLUSTER_RES) or
        not os.path.isfile(SERVICE_PASSWD_PATH)):
        return
    log('Synchronizing service passwords to all peers.')
    if is_clustered():
        unison.sync_to_peers(peer_interface='cluster',
                             paths=[SERVICE_PASSWD_PATH], user=SSH_USER,
                             verbose=True)

CA = []


def get_ca(user='keystone', group='keystone'):
    """
    Initialize a new CA object if one hasn't already been loaded.
    This will create a new CA or load an existing one.
    """
    if not CA:
        if not os.path.isdir(SSL_DIR):
            os.mkdir(SSL_DIR)
        d_name = '_'.join(SSL_CA_NAME.lower().split(' '))
        ca = ssl.JujuCA(name=SSL_CA_NAME, user=user, group=group,
                        ca_dir=os.path.join(SSL_DIR,
                                            '%s_intermediate_ca' % d_name),
                        root_ca_dir=os.path.join(SSL_DIR,
                                            '%s_root_ca' % d_name))
        # SSL_DIR is synchronized via all peers over unison+ssh, need
        # to ensure permissions.
        subprocess.check_output(['chown', '-R', '%s.%s' % (user, group),
                                '%s' % SSL_DIR])
        subprocess.check_output(['chmod', '-R', 'g+rwx', '%s' % SSL_DIR])
        CA.append(ca)
    return CA[0]


def relation_list(rid):
    cmd = [
        'relation-list',
        '-r', rid,
        ]
    result = str(subprocess.check_output(cmd)).split()
    if result == "":
        return None
    else:
        return result


def add_service_to_keystone(relation_id=None, remote_unit=None):
    settings = relation_get(rid=relation_id, unit=remote_unit)
    # the minimum settings needed per endpoint
    single = set(['service', 'region', 'public_url', 'admin_url',
                  'internal_url'])
    if single.issubset(settings):
        # other end of relation advertised only one endpoint
        if 'None' in [v for k, v in settings.iteritems()]:
            # Some backend services advertise no endpoint but require a
            # hook execution to update auth strategy.
            relation_data = {}
            # Check if clustered and use vip + haproxy ports if so
            if is_clustered():
                relation_data["auth_host"] = config('vip')
                relation_data["service_host"] = config('vip')
            else:
                relation_data["auth_host"] = unit_private_ip()
                relation_data["service_host"] = unit_private_ip()
            relation_data["auth_port"] = config('admin-port')
            relation_data["service_port"] = config('service-port')
            if config('https-service-endpoints') in ['True', 'true']:
                # Pass CA cert as client will need it to
                # verify https connections
                ca = get_ca(user=SSH_USER)
                ca_bundle = ca.get_ca_bundle()
                relation_data['https_keystone'] = 'True'
                relation_data['ca_cert'] = b64encode(ca_bundle)
            # Allow the remote service to request creation of any additional
            # roles. Currently used by Horizon
            for role in get_requested_roles(settings):
                log("Creating requested role: %s" % role)
                create_role(role)
            relation_set(relation_id=relation_id,
                         **relation_data)
            return
        else:
            ensure_valid_service(settings['service'])
            add_endpoint(region=settings['region'],
                         service=settings['service'],
                         publicurl=settings['public_url'],
                         adminurl=settings['admin_url'],
                         internalurl=settings['internal_url'])
            service_username = settings['service']
            https_cn = urlparse.urlparse(settings['internal_url'])
            https_cn = https_cn.hostname
    else:
        # assemble multiple endpoints from relation data. service name
        # should be prepended to setting name, ie:
        #  realtion-set ec2_service=$foo ec2_region=$foo ec2_public_url=$foo
        #  relation-set nova_service=$foo nova_region=$foo nova_public_url=$foo
        # Results in a dict that looks like:
        # { 'ec2': {
        #       'service': $foo
        #       'region': $foo
        #       'public_url': $foo
        #   }
        #   'nova': {
        #       'service': $foo
        #       'region': $foo
        #       'public_url': $foo
        #   }
        # }
        endpoints = {}
        for k, v in settings.iteritems():
            ep = k.split('_')[0]
            x = '_'.join(k.split('_')[1:])
            if ep not in endpoints:
                endpoints[ep] = {}
            endpoints[ep][x] = v
        services = []
        https_cn = None
        for ep in endpoints:
            # weed out any unrelated relation stuff Juju might have added
            # by ensuring each possible endpiont has appropriate fields
            #  ['service', 'region', 'public_url', 'admin_url', 'internal_url']
            if single.issubset(endpoints[ep]):
                ensure_valid_service(ep['service'])
                ep = endpoints[ep]
                add_endpoint(region=ep['region'], service=ep['service'],
                             publicurl=ep['public_url'],
                             adminurl=ep['admin_url'],
                             internalurl=ep['internal_url'])
                services.append(ep['service'])
                if not https_cn:
                    https_cn = urlparse.urlparse(ep['internal_url'])
                    https_cn = https_cn.hostname
        service_username = '_'.join(services)

    if 'None' in [v for k, v in settings.iteritems()]:
        return

    if not service_username:
        return

    token = get_admin_token()
    log("Creating service credentials for '%s'" % service_username)

    service_password = get_service_password(service_username)
    create_user(service_username, service_password, config('service-tenant'))
    grant_role(service_username, config('admin-role'),
               config('service-tenant'))

    # Allow the remote service to request creation of any additional roles.
    # Currently used by Swift and Ceilometer.
    for role in get_requested_roles(settings):
        log("Creating requested role: %s" % role)
        create_role(role, service_username,
                    config('service-tenant'))

    # As of https://review.openstack.org/#change,4675, all nodes hosting
    # an endpoint(s) needs a service username and password assigned to
    # the service tenant and granted admin role.
    # note: config('service-tenant') is created in utils.ensure_initial_admin()
    # we return a token, information about our API endpoints, and the generated
    # service credentials
    relation_data = {
        "admin_token": token,
        "service_host": config("hostname"),
        "service_port": config("service-port"),
        "auth_host": config("hostname"),
        "auth_port": config("admin-port"),
        "service_username": service_username,
        "service_password": service_password,
        "service_tenant": config('service-tenant'),
        "https_keystone": "False",
        "ssl_cert": "",
        "ssl_key": "",
        "ca_cert": ""
    }

    # Check if clustered and use vip + haproxy ports if so
    if is_clustered():
        relation_data["auth_host"] = config('vip')
        relation_data["service_host"] = config('vip')

    # generate or get a new cert/key for service if set to manage certs.
    if config('https-service-endpoints') in ['True', 'true']:
        ca = get_ca(user=SSH_USER)
        cert, key = ca.get_cert_and_key(common_name=https_cn)
        ca_bundle = ca.get_ca_bundle()
        relation_data['ssl_cert'] = b64encode(cert)
        relation_data['ssl_key'] = b64encode(key)
        relation_data['ca_cert'] = b64encode(ca_bundle)
        relation_data['https_keystone'] = 'True'
        if is_clustered():
            unison.sync_to_peers(peer_interface='cluster',
                                 paths=[SSL_DIR], user=SSH_USER, verbose=True)
    relation_set(relation_id=relation_id,
                 **relation_data)


def ensure_valid_service(service):
    if service not in valid_services.keys():
        log("Invalid service requested: '%s'" % service)
        relation_set(admin_token=-1)
        return


def add_endpoint(region, service, publicurl, adminurl, internalurl):
    desc = valid_services[service]["desc"]
    service_type = valid_services[service]["type"]
    create_service_entry(service, service_type, desc)
    create_endpoint_template(region=region, service=service,
                             publicurl=publicurl,
                             adminurl=adminurl,
                             internalurl=internalurl)


def get_requested_roles(settings):
    ''' Retrieve any valid requested_roles from dict settings '''
    if ('requested_roles' in settings and
        settings['requested_roles'] not in ['None', None]):
        return settings['requested_roles'].split(',')
    else:
        return []

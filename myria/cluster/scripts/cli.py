#!/usr/bin/env python

import sys
import os
import os.path
import signal
import traceback
import subprocess
from time import sleep
from tempfile import NamedTemporaryFile
from tempfile import mkdtemp
from collections import namedtuple
from string import ascii_lowercase
from operator import itemgetter
import click
import yaml
import json
import requests

import boto
import boto.ec2
import boto.vpc
import boto.iam
from boto.exception import EC2ResponseError
from boto.ec2.blockdevicemapping import BlockDeviceType, EBSBlockDeviceType, BlockDeviceMapping

# Ansible configuration variables to set before importing Ansible modules
os.environ['ANSIBLE_SSH_ARGS'] = "-o ControlMaster=auto -o ControlPersist=60s -o UserKnownHostsFile=/dev/null"
os.environ['ANSIBLE_RECORD_HOST_KEYS'] = "False"
os.environ['ANSIBLE_HOST_KEY_CHECKING'] = "False"
os.environ['ANSIBLE_SSH_PIPELINING'] = "True"
os.environ['ANSIBLE_RETRY_FILES_ENABLED'] = "True"
os.environ['ANSIBLE_NOCOWS'] = "True"

from ansible.inventory import Inventory
from ansible.vars import VariableManager
from ansible.parsing.dataloader import DataLoader
from ansible.executor import playbook_executor
from ansible.utils.display import Display
from ansible.plugins.callback import CallbackBase

import jinja2

from myria.cluster.playbooks import playbooks_dir

from distutils.spawn import find_executable
import pkg_resources
VERSION = pkg_resources.get_distribution("myria-cluster").version
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

SCRIPT_NAME =  os.path.basename(sys.argv[0])
# this is necessary because pip loses executable permissions and ansible requires scripts to be executable
INVENTORY_SCRIPT_PATH = find_executable("ec2.py")
# we want to use only the Ansible executable in our dependent package
ANSIBLE_EXECUTABLE_PATH = find_executable("ansible-playbook")

ANSIBLE_GLOBAL_VARS = yaml.load(file(os.path.join(playbooks_dir, "group_vars/all"), 'r'))
MAX_CONCURRENT_TASKS = 20 # more than this can trigger "too many open files" on Mac
MAX_RETRIES_DEFAULT = 5

USER = os.getenv('USER')
HOME = os.getenv('HOME')


# valid log4j log levels (https://logging.apache.org/log4j/1.2/apidocs/org/apache/log4j/Level.html)
LOG_LEVELS = ['OFF', 'FATAL', 'ERROR', 'WARN', 'DEBUG', 'TRACE', 'ALL']

ALL_REGIONS = [
    'us-west-2',
    'us-east-1',
    'us-west-1',
    'eu-west-1',
    'eu-central-1',
    'ap-northeast-1',
    'ap-northeast-2',
    'ap-southeast-1',
    'ap-southeast-2',
    'ap-south-1',
    'sa-east-1'
]

# these mappings are taken from http://uec-images.ubuntu.com/query/trusty/server/released.txt

DEFAULT_STOCK_HVM_AMI_IDS = {
    'us-west-2': "ami-9abea4fb",
    'us-east-1': "ami-fce3c696",
    'us-west-1': "ami-06116566",
    'eu-west-1': "ami-f95ef58a",
    'eu-central-1': "ami-87564feb",
    'ap-northeast-1': "ami-a21529cc",
    'ap-northeast-2': "ami-09dc1267",
    'ap-southeast-1': "ami-25c00c46",
    'ap-southeast-2': "ami-6c14310f",
    'ap-south-1': "ami-ac5238c3",
    'sa-east-1': "ami-0fb83963",
}
assert set(DEFAULT_STOCK_HVM_AMI_IDS.keys()).issubset(set(ALL_REGIONS))

DEFAULT_STOCK_PV_AMI_IDS = {
    'us-west-2': "ami-9dbea4fc",
    'us-east-1': "ami-b2e3c6d8",
    'us-west-1': "ami-42116522",
    'eu-west-1': "ami-be5cf7cd",
    'eu-central-1': "ami-d0574ebc",
    'ap-northeast-1': "ami-d91428b7",
    'ap-northeast-2': "ami-1bc10f75",
    'ap-southeast-1': "ami-a2c10dc1",
    'ap-southeast-2': "ami-530b2e30",
    'sa-east-1': "ami-feb73692",
}
assert set(DEFAULT_STOCK_PV_AMI_IDS.keys()).issubset(set(ALL_REGIONS))

DEFAULT_PROVISIONED_HVM_AMI_IDS = {
    'us-west-2': "ami-a06ea1c0",
    'us-east-1': "ami-c84dd9df",
    'us-west-1': "ami-c82666a8",
    'eu-west-1': "ami-c87e12bb",
    'eu-central-1': "ami-c944b0a6",
    'ap-northeast-1': "ami-139d5872",
    'ap-northeast-2': "ami-f908c297",
    'ap-southeast-1': "ami-dd0fd0be",
    'ap-southeast-2': "ami-752a1f16",
    'ap-south-1': "ami-3334415c",
    'sa-east-1': "ami-3b54c357",
}
assert set(DEFAULT_PROVISIONED_HVM_AMI_IDS.keys()).issubset(set(ALL_REGIONS))

DEFAULT_PROVISIONED_PV_AMI_IDS = {
    'us-west-2': "ami-7c60af1c",
    'us-east-1': "ami-8364f094",
    'us-west-1': "ami-952868f5",
    'eu-west-1': "ami-f94e228a",
    'eu-central-1': "ami-3947b356",
    'ap-northeast-1': "ami-ca9e5bab",
    'ap-northeast-2': "ami-e90bc187",
    'ap-southeast-1': "ami-770ed114",
    'ap-southeast-2': "ami-2c3d084f",
    'ap-south-1': "ami-d13643be",
    'sa-east-1': "ami-7555c219",
}
assert set(DEFAULT_PROVISIONED_PV_AMI_IDS.keys()).issubset(set(ALL_REGIONS))

DEVICE_PATH_PREFIX = "/dev/xvd"
PV_INSTANCE_TYPE_FAMILIES = ['c1', 'hi1', 'hs1', 'm1', 'm2', 't1']
LOCAL_STORAGE_INSTANCE_TYPE_FAMILIES = ['m1', 'm2', 'm3', 'c1', 'c3', 'r3', 'i2']
EPHEMERAL_VOLUMES_BY_INSTANCE_TYPE = {
  'c1.medium': 1,
  'c1.xlarge': 4,
  'c3.large': 2,
  'c3.xlarge': 2,
  'c3.2xlarge': 2,
  'c3.4xlarge': 2,
  'c3.8xlarge': 2,
  'i2.xlarge': 1,
  'i2.2xlarge': 2,
  'i2.4xlarge': 4,
  'm1.small': 1,
  'm1.medium': 1,
  'm1.large': 2,
  'm1.xlarge': 4,
  'm2.xlarge': 1,
  'm2.2xlarge': 1,
  'm2.4xlarge': 2,
  'm3.medium': 1,
  'm3.large': 1,
  'm3.xlarge': 2,
  'm3.2xlarge': 2,
  'r3.large': 1,
  'r3.xlarge': 1,
  'r3.2xlarge': 1,
  'r3.4xlarge': 1,
  'r3.8xlarge': 2,
}

SecurityGroupRule = namedtuple("SecurityGroupRule", ["ip_protocol", "from_port", "to_port", "cidr_ip", "src_group"])
ssh_port = 22
http_port = 80
https_port = 443
myria_rest_port = ANSIBLE_GLOBAL_VARS['myria_rest_port']
myria_web_port = ANSIBLE_GLOBAL_VARS['myria_web_port']
ganglia_web_port = ANSIBLE_GLOBAL_VARS['ganglia_web_port']
jupyter_web_port = ANSIBLE_GLOBAL_VARS['jupyter_web_port']
resourcemanager_web_port = ANSIBLE_GLOBAL_VARS['resourcemanager_web_port']
nodemanager_web_port = ANSIBLE_GLOBAL_VARS['nodemanager_web_port']
SECURITY_GROUP_RULES = [
    SecurityGroupRule("tcp", ssh_port, ssh_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", http_port, http_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", https_port, https_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", myria_rest_port, myria_rest_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", myria_web_port, myria_web_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", ganglia_web_port, ganglia_web_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", jupyter_web_port, jupyter_web_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", resourcemanager_web_port, resourcemanager_web_port, "0.0.0.0/0", None),
    SecurityGroupRule("tcp", nodemanager_web_port, nodemanager_web_port, "0.0.0.0/0", None),
]

DEFAULTS = dict(
    key_pair="%s-myria" % USER,
    region='us-west-2',
    instance_type='t2.large',
    cluster_size=5,
    storage_type='ebs',
    data_volume_size_gb=20,
    data_volume_type='gp2',
    data_volume_count=1,
    driver_mem_gb=0.5,
    coordinator_mem_gb=5.5,
    worker_mem_gb=5.5,
    heap_mem_fraction=0.9,
    coordinator_vcores=1,
    worker_vcores=1,
    node_mem_gb=6.0,
    node_vcores=2,
    workers_per_node=1,
    cluster_log_level='WARN'
)


def create_key_pair_and_private_key_file(key_pair, private_key_file, region, profile=None, verbosity=0):
    # First, check if private key file exists and is readable
    if verbosity > 0:
        click.echo("Checking for existence/readability of private key file '%s'..." % private_key_file)
    private_key_exists = (os.path.isfile(private_key_file) and os.access(private_key_file, os.R_OK))
    ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
    try:
        if verbosity > 0:
            click.echo("Checking for existence of key pair '%s'..." % key_pair)
        key = ec2.get_all_key_pairs(keynames=[key_pair])[0]
    except ec2.ResponseError as e:
        if e.code == 'InvalidKeyPair.NotFound':
            # Fail if key pair doesn't exist but private key file already exists
            if private_key_exists:
                click.echo("""
Key pair '{key_pair}' not found, but private key file '{private_key_file}' already exists!
Please delete or rename it, delete the key pair '{key_pair}' from the {region} region, and rerun the script.
""".format(key_pair=key_pair, private_key_file=private_key_file, region=region))
                sys.exit(1)
            if verbosity > 0:
                click.echo("Key pair '%s' not found, creating..." % key_pair)
            key = ec2.create_key_pair(key_pair)
            if verbosity > 0:
                click.echo("Saving private key for key pair '%s' to file '%s'..." % (key_pair, private_key_file))
            key_dir = os.path.dirname(private_key_file)
            key.save(key_dir)
            # key.save() creates file with hardcoded name <key_pair>.pem
            os.rename(os.path.join(key_dir, "%s.pem" % key_pair), private_key_file)
        else:
            raise
    else:
        # Fail if key pair already exists but private key file is missing
        if not private_key_exists:
            click.echo("""
Key pair '{key_pair}' exists in the {region} region but private key file '{private_key_file}' is missing!
Either 1) use a different key pair, 2) copy the private key file for the key pair '{key_pair}' to '{private_key_file}',
or 3) delete the key pair '{key_pair}' from the {region} region, and rerun the script.
""".format(key_pair=key_pair, private_key_file=private_key_file, region=region))
            sys.exit(1)


def write_inventory_file(cluster_name, region, profile, verbosity=0):
    ec2_ini_tmpfile = NamedTemporaryFile(delete=False)
    if verbosity > 0:
        click.echo("Writing Ansible dynamic inventory file to %s" % ec2_ini_tmpfile.name)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(playbooks_dir))
    template = env.get_template("ec2.ini.j2")
    template_args = dict(REGION=region, CLUSTER_NAME=cluster_name)
    # We can't pass in None for a missing profile or the template won't behave correctly.
    if profile:
        template_args.update(PROFILE=profile)
    ec2_ini_tmpfile.write(template.render(template_args))
    # THIS IS CRITICAL (ec2.py won't see full file contents otherwise)
    ec2_ini_tmpfile.flush()
    return ec2_ini_tmpfile.name


def launch_cluster(cluster_name, verbosity=0, **kwargs):
    # Create EC2 key pair if absent
    create_key_pair_and_private_key_file(kwargs['key_pair'], kwargs['private_key_file'], kwargs['region'],
            profile=kwargs['profile'], verbosity=verbosity)
    # Create security group for this cluster
    group = create_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=kwargs['vpc_id'], verbosity=verbosity)
    # Tag security group to designate as Myria cluster
    group_tags = {'app': "myria", 'storage-type': kwargs['storage_type']}
    if kwargs['iam_user']:
        group_tags.update({'user:Name': kwargs['iam_user']})
    if kwargs['spot_price']:
        group_tags.update({'spot-price': kwargs['spot_price']})
    group.add_tags(group_tags)
    # Allow this group complete access to itself
    self_rules = [SecurityGroupRule(proto, 0, 65535, "0.0.0.0/0", group) for proto in ['tcp', 'udp']]
    rules = self_rules + SECURITY_GROUP_RULES
    # Add security group rules
    for rule in rules:
        group.authorize(ip_protocol=rule.ip_protocol,
                        from_port=rule.from_port,
                        to_port=rule.to_port,
                        cidr_ip=rule.cidr_ip,
                        src_group=rule.src_group)
    # Launch instances
    if verbosity > 0:
        click.echo("Launching instances...")
    ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    launch_args=dict(image_id=kwargs['ami_id'],
                     key_name=kwargs['key_pair'],
                     security_groups=[group.name],
                     instance_type=kwargs['instance_type'],
                     placement=kwargs['zone'],
                     subnet_id=kwargs['subnet_id'],
                     block_device_map=kwargs.get('device_mapping'),
                     instance_profile_name=kwargs['role'],
                     ebs_optimized=(kwargs['storage_type'] == 'ebs'))
    launched_instances = []
    if kwargs.get('spot_price'):
        launched_instance_ids = []
        launch_args.update(price=kwargs['spot_price'],
                           count=kwargs['cluster_size'],
                           launch_group="launch-group-%s" % cluster_name, # fate-sharing across instances
                           availability_zone_group="az-launch-group-%s" % cluster_name) # launch all instances in same AZ
        spot_requests = ec2.request_spot_instances(**launch_args)
        spot_request_ids = [req.id for req in spot_requests]
        while True:
            # Spot request objects won't auto-update, so we need to fetch them again on each iteration.
            for req in ec2.get_all_spot_instance_requests(request_ids=spot_request_ids):
                print req.state
                print req.status.code
                if req.state != "active":
                    break
                else:
                    launched_instance_ids.append(req.instance_id)
            else: # all requests fulfilled, so break out of while loop
                break
            if verbosity > 0:
                click.echo("Not all spot requests fulfilled, waiting 60 seconds...")
            sleep(60)

        reservations = ec2.get_all_instances(launched_instance_ids)
        launched_instances = [instance for res in reservations for instance in res.instances]
    else:
        launch_args.update(min_count=kwargs['cluster_size'], max_count=kwargs['cluster_size'])
        reservation = ec2.run_instances(**launch_args)
        launched_instances = reservation.instances
    # Tag instances
    if verbosity > 0:
        click.echo("Tagging instances...")
    instances = sorted((instance for instance in launched_instances), key=lambda i: i.private_dns_name)
    for idx, instance in enumerate(instances):
        instance_tags = {'app': "myria", 'cluster-name': cluster_name}
        if kwargs['iam_user']:
            instance_tags.update({'user:Name': kwargs['iam_user']})
        if kwargs['spot_price']:
            instance_tags.update({'spot-price': kwargs['spot_price']})
        instance.add_tags(instance_tags)
        # Tag volumes
        volumes = ec2.get_all_volumes(filters={'attachment.instance-id': instance.id})
        for volume in volumes:
            volume_tags = {'app': "myria", 'cluster-name': cluster_name}
            if kwargs['iam_user']:
                volume_tags.update('user:Name', kwargs['iam_user'])
            volume.add_tags(volume_tags)
        if idx == 0:
            # Tag coordinator
            instance.add_tags({'cluster-role': "coordinator", 'worker-id': "0"})
        else:
            # Tag workers
            worker_id_tag = ','.join(map(str, range(((idx - 1) * kwargs['workers_per_node']) + 1, (idx  * kwargs['workers_per_node']) + 1)))
            instance.add_tags({'cluster-role': "worker", 'worker-id': worker_id_tag})


def get_security_group_for_cluster(cluster_name, region, profile=None, vpc_id=None):
    ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
    groups = []
    if vpc_id:
        # In the EC2 API, filters can only express OR,
        # so we have to implement AND by intersecting results for each filter.
        groups_in_vpc = ec2.get_all_security_groups(filters={'vpc-id': vpc_id})
        groups = [g for g in groups_in_vpc if g.name == cluster_name]
    else:
        try:
            groups = ec2.get_all_security_groups(groupnames=cluster_name)
        except ec2.ResponseError as e:
            if e.code == 'InvalidGroup.NotFound':
                return None
            else:
                raise
    if not groups:
        return None
    else:
        return groups[0]


def create_security_group_for_cluster(cluster_name, region, profile=None, vpc_id=None, verbosity=0):
    if verbosity > 0:
        click.echo("Creating security group '%s' in region '%s'..." % (cluster_name, region))
    ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
    group = ec2.create_security_group(cluster_name, "Myria security group", vpc_id=vpc_id)
    # We need to poll for availability after creation since as usual AWS is eventually consistent
    while True:
        try:
            new_group = ec2.get_all_security_groups(group_ids=[group.id])[0]
        except ec2.ResponseError as e:
            if e.code == 'InvalidGroup.NotFound':
                if verbosity > 0:
                    click.echo("Waiting for security group '%s' in region '%s' to become available..." % (cluster_name, region))
                sleep(5)
            else:
                raise
        else:
            break
    return group


def terminate_cluster(cluster_name, region, profile=None, vpc_id=None):
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    if not group:
        click.echo("Security group '%s' not found" % cluster_name)
        return
    instance_ids = [instance.id for instance in group.instances()]
    # we want to allow users to delete a security group with no instances
    if instance_ids:
        click.echo("Terminating instances %s" % ', '.join(instance_ids))
        ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
        ec2.terminate_instances(instance_ids=instance_ids)
    click.echo("Deleting security group '%s' (%s)" % (group.name, group.id))
    # EC2 can take a while to update dependencies, so retry until we succeed
    while True:
        try:
            group.delete()
        except EC2ResponseError as e:
            if e.error_code == "DependencyViolation":
                click.echo("Security group state still converging...")
                sleep(5)
            else:
                raise
        else:
            click.echo("Security group '%s' (%s) successfully deleted" % (group.name, group.id))
            break


def get_coordinator_public_hostname(cluster_name, region, profile=None, vpc_id=None):
    coordinator_hostname = None
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    for instance in group.instances():
        if instance.tags.get('cluster-role') == "coordinator":
            coordinator_hostname = instance.public_dns_name
            break
    return coordinator_hostname


def get_worker_public_hostnames(cluster_name, region, profile=None, vpc_id=None):
    worker_hostnames = []
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    for instance in group.instances():
        if instance.tags.get('cluster-role') == "worker":
            worker_hostnames.append(instance.public_dns_name)
    return worker_hostnames


def wait_for_all_instances_reachable(cluster_name, region, profile=None, vpc_id=None, verbosity=0):
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    instance_ids = [instance.id for instance in group.instances()]
    while True:
        ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
        statuses = ec2.get_all_instance_status(instance_ids=instance_ids, include_all_instances=True)
        for status in statuses:
            if status.state_name != "running":
                break
            if status.instance_status.status != "ok":
                break
            if status.instance_status.details['reachability'] != "passed":
                break
        else: # all instances reachable, so break out of while loop
            break
        if verbosity > 0:
            click.echo("Not all instances reachable, waiting 60 seconds...")
        sleep(60)


def wait_for_all_workers_online(cluster_name, region, profile=None, vpc_id=None, verbosity=0):
    coordinator_hostname = get_coordinator_public_hostname(cluster_name, region, profile=profile, vpc_id=vpc_id)
    workers_url = "http://%(host)s:%(port)d/workers" % dict(host=coordinator_hostname, port=ANSIBLE_GLOBAL_VARS['myria_rest_port'])
    while True:
        try:
            workers_resp = requests.get(workers_url)
        except requests.ConnectionError:
            if verbosity > 0:
                click.echo("Myria service unavailable, waiting 60 seconds...")
        else:
            if workers_resp.status_code == requests.codes.ok:
                workers = workers_resp.json()
                workers_alive_resp = requests.get(workers_url + "/alive")
                workers_alive = workers_alive_resp.json()
                if len(workers_alive) == len(workers):
                    break
                else:
                    if verbosity > 0:
                        click.echo("Not all Myria workers online (%d/%d), waiting 60 seconds..." % (
                            len(workers_alive), len(workers)))
            else:
                click.echo("Error response from Myria service (status code %d):\n%s" % (
                    workers_resp.status_code, workers_resp.text))
                return False
        sleep(60)
    return True


def instance_type_family_from_instance_type(instance_type):
    return instance_type.split('.')[0]


def default_key_file_from_key_pair(ctx, param, value):
    if value is None:
        qualified_key_pair = "%s_%s" % (ctx.params['key_pair'], ctx.params['region'])
        if ctx.params['profile']:
            qualified_key_pair = "%s_%s_%s" % (ctx.params['key_pair'], ctx.params['profile'], ctx.params['region'])
        return "%s/.ssh/%s.pem" % (HOME, qualified_key_pair)
    else:
        return value


def default_ami_id_from_region(ctx, param, value):
    if value is None:
        ami_id = None
        use_stock_ami = ctx.params['unprovisioned']
        instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
        if instance_type_family in PV_INSTANCE_TYPE_FAMILIES:
            ami_id = DEFAULT_STOCK_PV_AMI_IDS.get(ctx.params['region']) if use_stock_ami else DEFAULT_PROVISIONED_PV_AMI_IDS.get(ctx.params['region'])
        else:
            ami_id = DEFAULT_STOCK_HVM_AMI_IDS.get(ctx.params['region']) if use_stock_ami else DEFAULT_PROVISIONED_HVM_AMI_IDS.get(ctx.params['region'])
        if ami_id is None:
            raise click.BadParameter("No default %s AMI found for instance type '%s' in region '%s'" % (
                ("unprovisioned" if use_stock_ami else "provisioned"), ctx.params['instance_type'], ctx.params['region']))
        return ami_id
    else:
        return value


def validate_subnet_id(ctx, param, value):
    if value is not None:
        if ctx.params.get('zone') is not None:
            raise click.BadParameter("Cannot specify --zone if --subnet-id is specified")
        vpc_conn = boto.vpc.connect_to_region(ctx.params['region'], profile_name=ctx.params.get('profile'))
        try:
            subnet = vpc_conn.get_all_subnets(subnet_ids=[value])[0]
            ctx.params['vpc_id'] = subnet.vpc_id
        except:
            raise click.BadParameter("Invalid subnet ID '%s' for region '%s'" % (value, ctx.params['region']))
        return value


def validate_console_logging(ctx, param, value):
    if value is True:
        if ctx.params.get('silent') or ctx.params.get('verbose'):
            raise click.BadParameter("Cannot specify both --silent and --verbose")
    return value


def validate_aws_settings(region, profile=None, vpc_id=None, verbosity=0):
    # abort if credentials are not available
    try:
        boto.ec2.connect_to_region(region, profile_name=profile)
    # except:
    except Exception as e:
        if verbosity > 0:
            click.echo(e)
        click.echo("""
Unable to connect to the '{region}' EC2 region using the '{profile}' profile.
Please ensure that your AWS credentials are correctly configured:

http://boto3.readthedocs.io/en/latest/guide/configuration.html
""".format(region=region, profile=profile if profile else "default"))
        sys.exit(1)

    vpc_conn = boto.vpc.connect_to_region(region, profile_name=profile)
    # abort if VPC is not specified and no default VPC exists
    if not vpc_id:
        default_vpcs = vpc_conn.get_all_vpcs(filters={'isDefault': "true"})
        if not default_vpcs:
            click.echo("""
No default VPC is configured for your AWS account in the '{region}' region.
Please ask your administrator to create a default VPC or specify a VPC subnet using the `--subnet-id` option.
""".format(region=region))
            sys.exit(1)
    else:
        # verify that specified VPC exists
        try:
            vpc_conn.get_all_vpcs(vpc_ids=[vpc_id])
        except EC2ResponseError as e:
            if e.error_code == "InvalidVpcID.NotFound":
                click.echo("""
No VPC found with ID '{vpc_id}' in the '{region}' region.
""".format(region=region, vpc_id=vpc_id))
                sys.exit(1)


def validate_region(ctx, param, value):
    if value is not None:
        if value not in ALL_REGIONS:
            raise click.BadParameter("Region must be one of the following:\n%s" % '\n'.join(ALL_REGIONS))
    return value


def validate_storage_type(ctx, param, value):
    if value == "local":
        instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
        if instance_type_family not in LOCAL_STORAGE_INSTANCE_TYPE_FAMILIES:
            raise click.BadParameter("Instance type %s is incompatible with local storage" % ctx.params['instance_type'])
    return value


def validate_volume_type(ctx, param, value):
    if value is not None:
        if ctx.params.get('storage_type') == "local":
            raise click.BadParameter("Cannot specify volume type with --storage-type=local")
    return value


def validate_volume_count(ctx, param, value):
    if value is not None:
        if ctx.params.get('storage_type') == "local":
            raise click.BadParameter("Cannot specify volume count with --storage-type=local")
    elif ctx.params.get('storage_type') == "ebs":
        return DEFAULTS['data_volume_count']
    return value


def validate_volume_size(ctx, param, value):
    if value is not None:
        if ctx.params.get('storage_type') == "local":
            raise click.BadParameter("Cannot specify volume size with --storage-type=local")
    elif ctx.params.get('storage_type') == "ebs":
        return DEFAULTS['data_volume_size_gb']
    return value


def validate_volume_iops(ctx, param, value):
    if value is not None:
        if ctx.params.get('data_volume_type') != "io1":
            raise click.BadParameter("--data-volume-iops can only be specified with 'io1' volume type")
    return value


def validate_data_volume_count(ctx, param, value):
    if value is not None:
        if ctx.params.get('storage_type') == "local":
            raise click.BadParameter("Cannot specify --data-volume-count with --storage-type=local")
        if value > ctx.params.get('workers_per_node'):
            raise click.BadParameter("--data-volume-count cannot exceed number of workers per node")
    return value


def get_iam_user(region, profile=None, verbosity=0):
    # extract IAM user name for resource tagging
    iam_conn = boto.iam.connect_to_region(region, profile_name=profile)
    iam_user = None
    try:
        # TODO: once we move to boto3, we can get better info on callling principal from boto3.sts.get_caller_identity()
        iam_user = iam_conn.get_user()['get_user_response']['get_user_result']['user']['user_name']
    except:
        pass
    if not iam_user and verbosity > 0:
        click.echo("Warning: unable to find IAM user with credentials provided. IAM user tagging will be disabled.")
    return iam_user


def get_block_device_mapping(**kwargs):
    # Create block device mapping
    device_mapping = BlockDeviceMapping()
    # Generate all local volume mappings
    num_local_volumes = EPHEMERAL_VOLUMES_BY_INSTANCE_TYPE.get(kwargs['instance_type'], 0)
    for local_dev_idx in xrange(num_local_volumes):
        local_dev = BlockDeviceType()
        local_dev.ephemeral_name = "%s%d" % ("ephemeral", local_dev_idx)
        local_dev_letter = ascii_lowercase[1+local_dev_idx]
        local_dev_name = "%s%s" % (DEVICE_PATH_PREFIX, local_dev_letter)
        device_mapping[local_dev_name] = local_dev
    # Generate all EBS volume mappings
    if kwargs['storage_type'] == 'ebs':
        num_ebs_volumes = kwargs['data_volume_count']
        for ebs_dev_idx in xrange(num_ebs_volumes):
            ebs_dev = EBSBlockDeviceType()
            ebs_dev.size = kwargs['data_volume_size_gb']
            ebs_dev.delete_on_termination = True
            ebs_dev.volume_type = kwargs['data_volume_type']
            ebs_dev.iops = kwargs['data_volume_iops']
            # We always have one root volume and 0 to 4 ephemeral volumes.
            ebs_dev_letter = ascii_lowercase[1+num_local_volumes+ebs_dev_idx]
            ebs_dev_name = "%s%s" % (DEVICE_PATH_PREFIX, ebs_dev_letter)
            device_mapping[ebs_dev_name] = ebs_dev
    return device_mapping


# If this is called with `local=False`, then the key `EC2_INI_PATH` in `extra_vars`
# must be set to a valid instance of the template `myria/cluster/playbooks/ec2.ini.j2`.
def run_playbook(playbook, private_key_file, local=True, extra_vars={}, tags=[], max_retries=MAX_RETRIES_DEFAULT, destroy_cluster_on_failure=True, verbosity=0):
    # this should be done in an env var but Ansible maintainers are too stupid to support it
    extra_vars.update(ansible_python_interpreter='/usr/bin/env python')
    cluster_name = extra_vars['CLUSTER_NAME']
    region = extra_vars['REGION']
    profile = extra_vars.get('PROFILE')
    vpc_id = extra_vars.get('VPC_ID')
    playbook_path = os.path.join(playbooks_dir, playbook)
    inventory = "localhost," if local else INVENTORY_SCRIPT_PATH # comma is not a typo, Ansible is just stupid
    # Override default retry files directory
    ansible_retry_tmpdir = mkdtemp()
    retry_filename = os.path.join(ansible_retry_tmpdir, os.path.splitext(os.path.basename(playbook))[0] + ".retry")
    env = dict(os.environ, ANSIBLE_RETRY_FILES_SAVE_PATH=ansible_retry_tmpdir)
    if 'EC2_INI_PATH' in extra_vars:
        env.update(EC2_INI_PATH=extra_vars['EC2_INI_PATH'])
    # see https://github.com/ansible/ansible/pull/9404/files
    retries = 0
    failed_hosts = []
    while True:
        # ansible_args = [ANSIBLE_EXECUTABLE_PATH, playbook_path, "--inventory", inventory, "--extra-vars", json.dumps(extra_vars), "--private-key", private_key_file]
        ansible_args = [ANSIBLE_EXECUTABLE_PATH, playbook_path, "--inventory", inventory, "--extra-vars", json.dumps(extra_vars), "--private-key", private_key_file]
        if tags:
            ansible_args.extend(["--tags", ','.join(tags)])
        if failed_hosts:
            ansible_args.extend(["--limit", "@%s" % retry_filename])
        if verbosity > 0:
            ansible_args.append("-" + ('v' * verbosity))
        status = subprocess.call(ansible_args, env=env)
        # handle failure
        if status != 0:
            if status in [2, 3]: # failed tasks or unreachable hosts respectively
                if retries < max_retries:
                    retries += 1
                    failed_hosts = []
                    with open(retry_filename,'r') as f:
                        failed_hosts = f.read().splitlines() 
                    assert(failed_hosts) # should always have at least one failed host with these exit codes
                    click.echo("Playbook run failed on hosts %s, retrying (%d of %d)..." % (', '.join(failed_hosts), retries, max_retries))
                    continue
                else:
                    click.echo("Exceeded maximum %d retries, exiting." % max_retries)
            else:
                click.echo("Unexpected Ansible error, exiting.")
            if destroy_cluster_on_failure:
                click.echo("Destroying cluster...")
                terminate_cluster(cluster_name, region=region, profile=profile, vpc_id=vpc_id)
            sys.exit(1)
        else:
            break


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=VERSION)
def run():
    pass


@run.command('create')
@click.argument('cluster_name')
@click.option('--verbose', is_flag=True, callback=validate_console_logging)
@click.option('--silent', is_flag=True, callback=validate_console_logging)
@click.option('--unprovisioned', is_flag=True, is_eager=True, help="Install required software at deployment")
@click.option('--profile', default=None, is_eager=True,
    help="AWS credential profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], is_eager=True, callback=validate_region,
    help="AWS region to launch your cluster in")
@click.option('--zone', show_default=True, default=None, is_eager=True,
    help="AWS availability zone to launch your cluster in")
@click.option('--key-pair', show_default=True, default=DEFAULTS['key_pair'],
    help="EC2 key pair used to launch your cluster")
@click.option('--private-key-file', callback=default_key_file_from_key_pair,
    help="Private key file for your EC2 key pair [default: %s]" % ("%s/.ssh/%s-myria_%s.pem" % (HOME, USER, DEFAULTS['region'])))
@click.option('--instance-type', show_default=True, default=DEFAULTS['instance_type'], is_eager=True,
    help="EC2 instance type for your cluster")
@click.option('--cluster-size', show_default=True, default=DEFAULTS['cluster_size'],
    type=click.IntRange(3, 1000), help="Number of EC2 instances in your cluster")
@click.option('--ami-id', callback=default_ami_id_from_region,
    help="ID of the AMI (Amazon Machine Image) used for your EC2 instances [default: %s]" % DEFAULT_PROVISIONED_HVM_AMI_IDS[DEFAULTS['region']])
@click.option('--subnet-id', default=None, callback=validate_subnet_id,
    help="ID of the VPC subnet in which to launch your EC2 instances")
@click.option('--role', help="Name of an IAM role used to launch your EC2 instances")
@click.option('--spot-price', help="Price in dollars of the maximum bid for an EC2 spot instance request")
@click.option('--storage-type', show_default=True, callback=validate_storage_type, is_eager=True,
    type=click.Choice(['ebs', 'local']), default=DEFAULTS['storage_type'],
    help="Type of the block device where Myria data is stored")
@click.option('--data-volume-size-gb', show_default=True, default=DEFAULTS['data_volume_size_gb'],
    callback=validate_volume_size, help="Size of each EBS data volume in GB")
@click.option('--data-volume-type', show_default=True, default=DEFAULTS['data_volume_type'],
    type=click.Choice(['gp2', 'io1', 'st1', 'sc1']), callback=validate_volume_type,
    help="EBS data volume type: General Purpose SSD (gp2), Provisioned IOPS SSD (io1), Throughput Optimized HDD (st1), Cold HDD (sc1)")
@click.option('--data-volume-iops', type=int, default=None, callback=validate_volume_iops,
    help="IOPS to provision for each EBS data volume (only applies to 'io1' volume type)")
@click.option('--driver-mem-gb', show_default=True, default=DEFAULTS['driver_mem_gb'],
    help="Physical memory (in GB) reserved for Myria driver")
@click.option('--coordinator-mem-gb', show_default=True, default=DEFAULTS['coordinator_mem_gb'],
    help="Physical memory (in GB) reserved for Myria coordinator")
@click.option('--worker-mem-gb', show_default=True, default=DEFAULTS['worker_mem_gb'],
    help="Physical memory (in GB) reserved for each Myria worker")
@click.option('--heap-mem-fraction', show_default=True, default=DEFAULTS['heap_mem_fraction'],
    help="Fraction of container memory used for JVM heap")
@click.option('--coordinator-vcores', show_default=True, default=DEFAULTS['coordinator_vcores'],
    help="Number of virtual CPUs reserved for Myria coordinator")
@click.option('--worker-vcores', show_default=True, default=DEFAULTS['worker_vcores'],
    help="Number of virtual CPUs reserved for each Myria worker")
@click.option('--node-mem-gb', show_default=True, default=DEFAULTS['node_mem_gb'],
    help="Physical memory (in GB) on each EC2 instance available for Myria processes")
@click.option('--node-vcores', show_default=True, default=DEFAULTS['node_vcores'],
    help="Number of virtual CPUs on each EC2 instance available for Myria processes")
@click.option('--workers-per-node', show_default=True, default=DEFAULTS['workers_per_node'],
    help="Number of Myria workers per cluster node")
@click.option('--data-volume-count', type=click.IntRange(1, 8), show_default=True, default=DEFAULTS['data_volume_count'],
    callback=validate_data_volume_count, help="Number of EBS data volumes to attach to this instance")
@click.option('--cluster-log-level', show_default=True,
    type=click.Choice(LOG_LEVELS), default=DEFAULTS['cluster_log_level'])
def create_cluster(cluster_name, **kwargs):
    verbosity = 3 if kwargs['verbose'] else 0 if kwargs['silent'] else 1
    vpc_id = kwargs.get('vpc_id')
    iam_user = get_iam_user(kwargs['region'], profile=kwargs['profile'], verbosity=verbosity)

    # for displaying example commands
    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if vpc_id:
        options_str += " --vpc-id %s" % vpc_id

    validate_aws_settings(kwargs['region'], kwargs['profile'], vpc_id, verbosity=verbosity)

    # abort if cluster already exists
    if get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id):
        click.echo("""
Cluster '{cluster_name}' already exists in the '{region}' region. If you wish to create a new cluster with the same name, first run

{script_name} destroy {cluster_name} {options}
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))
        sys.exit(1)

    device_mapping = get_block_device_mapping(**kwargs)
    # We need to massage opaque BlockDeviceType objects into dicts we can pass to Ansible
    all_volumes = [dict(v.__dict__.iteritems(), device_name=k) for k, v in sorted(device_mapping.iteritems(), key=itemgetter(0))]
    ebs_volumes = all_volumes[-kwargs['data_volume_count']:]
    ephemeral_volumes = all_volumes[0:-kwargs['data_volume_count']]

    # install keyboard interrupt handler to destroy partially-deployed cluster
    # TODO: signal handlers are inherited by each child process spawned by Ansible,
    # so messages are (harmlessly) duplicated for each process.
    def signal_handler(sig, frame):
        # ignore future interrupts
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        click.echo("User interrupted deployment, destroying cluster...")
        try:
            terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        except:
            pass # best-effort
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)
    try:
        # launch all instances in this cluster
        launch_cluster(cluster_name, vpc_id=vpc_id, iam_user=iam_user, device_mapping=device_mapping, verbosity=verbosity, **kwargs)
    except Exception as e:
        if verbosity > 0:
            click.echo(e)
        if verbosity > 1:
            click.echo(traceback.format_exc())
        click.echo("Unexpected error, destroying cluster...")
        terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        sys.exit(1)

    # poll instances for status until all are reachable
    if verbosity > 0:
        click.echo("Waiting for all instances to become reachable...")
    wait_for_all_instances_reachable(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=vpc_id, verbosity=verbosity)
    # Write ec2.ini file for dynamic inventory
    ec2_ini_file = write_inventory_file(cluster_name, kwargs['region'], kwargs['profile'], verbosity=verbosity)
    # run remote playbook to provision EC2 instances
    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(CLUSTER_NAME=cluster_name)
    extra_vars.update(VPC_ID=vpc_id)
    if iam_user:
        extra_vars.update(IAM_USER=iam_user)
    extra_vars.update(ALL_VOLUMES=all_volumes)
    extra_vars.update(EBS_VOLUMES=ebs_volumes)
    extra_vars.update(EPHEMERAL_VOLUMES=ephemeral_volumes)
    extra_vars.update(EC2_INI_PATH=ec2_ini_file)

    if verbosity > 2:
        click.echo(json.dumps(extra_vars))

    all_provisioned_ami_ids = DEFAULT_PROVISIONED_HVM_AMI_IDS.values() + DEFAULT_PROVISIONED_PV_AMI_IDS.values()
    tags = ['configure'] if kwargs['ami_id'] in all_provisioned_ami_ids else ['provision', 'configure']
    run_playbook("remote.yml", kwargs['private_key_file'], local=False, extra_vars=extra_vars, tags=tags, verbosity=verbosity)

    # wait for all workers to become available
    if verbosity > 0:
        click.echo("Waiting for Myria service to become available...")
    if not wait_for_all_workers_online(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=vpc_id, verbosity=verbosity):
        print("""
The Myria service on your cluster '{cluster_name}' in the '{region}' region returned an error.
Please refer to the error message above for diagnosis. Exiting (not destroying cluster).
""".format(cluster_name=cluster_name, region=kwargs['region']))
        sys.exit(1)

    coordinator_public_hostname = None
    try:
        coordinator_public_hostname = get_coordinator_public_hostname(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except:
        pass
    if not coordinator_public_hostname:
        click.echo("Couldn't resolve coordinator public DNS, exiting (not destroying cluster)")
        sys.exit(1)

    click.echo("""
Your new Myria cluster '{cluster_name}' has been launched on Amazon EC2 in the '{region}' region.

View the Myria worker IDs and public hostnames of all nodes in this cluster (the coordinator has worker ID 0):
{script_name} list {cluster_name} {options}
""" + (
"""Stop this cluster:
{script_name} stop {cluster_name} {options}

Start this cluster after stopping it:
{script_name} start {cluster_name} {options}
""" if not (kwargs.get('spot_price') or (kwargs['storage_type'] == "local")) else "") +
"""
Destroy this cluster:
{script_name} destroy {cluster_name} {options}

Log into the coordinator node:
ssh -i {private_key_file} {remote_user}@{coordinator_public_hostname}

myria-web interface:
http://{coordinator_public_hostname}:{myria_web_port}

MyriaX REST endpoint:
http://{coordinator_public_hostname}:{myria_rest_port}

Ganglia web interface:
http://{coordinator_public_hostname}:{ganglia_web_port}

Jupyter notebook interface:
http://{coordinator_public_hostname}:{jupyter_web_port}
""".format(coordinator_public_hostname=coordinator_public_hostname, myria_web_port=ANSIBLE_GLOBAL_VARS['myria_web_port'],
           myria_rest_port=ANSIBLE_GLOBAL_VARS['myria_rest_port'], ganglia_web_port=ANSIBLE_GLOBAL_VARS['ganglia_web_port'],
           jupyter_web_port=ANSIBLE_GLOBAL_VARS['jupyter_web_port'], private_key_file=kwargs['private_key_file'],
           remote_user=ANSIBLE_GLOBAL_VARS['remote_user'], script_name=SCRIPT_NAME, cluster_name=cluster_name,
           region=kwargs['region'], options=options_str))


@run.command('destroy')
@click.argument('cluster_name')
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], callback=validate_region,
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def destroy_cluster(cluster_name, **kwargs):
    try:
        terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    except ValueError as e:
        click.echo(e.message)
        sys.exit(1)


@run.command('stop')
@click.argument('cluster_name')
@click.option('--silent', is_flag=True)
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'],
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def stop_cluster(cluster_name, **kwargs):
    verbosity = 0 if kwargs['silent'] else 1
    group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    if group.tags.get('storage-type') == "local":
        click.echo("Cluster '%s' has storage type 'local' and cannot be stopped.")
        sys.exit(1)
    if group.tags.get('spot-price'):
        click.echo("Cluster '%s' has spot instances and cannot be stopped.")
        sys.exit(1)
    instance_ids = [instance.id for instance in group.instances()]
    if verbosity > 0:
        click.echo("Stopping instances %s" % ', '.join(instance_ids))
    ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    ec2.stop_instances(instance_ids=instance_ids)
    while True:
        for instance in group.instances():
            instance.update(validate=True)
            if instance.state != "stopped":
                if verbosity > 0:
                    click.echo("Instance %s not stopped, waiting 60 seconds..." % instance.id)
                sleep(60)
                break # break out of for loop
        else: # all instances were stopped, so break out of while loop
            break

    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if kwargs['vpc_id']:
        options_str += " --vpc-id %s" % kwargs['vpc_id']
    print("""
Your Myria cluster '{cluster_name}' in the AWS '{region}' region has been successfully stopped.
You can start this cluster again by running

{script_name} start {cluster_name} {options}
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))


@run.command('start')
@click.argument('cluster_name')
@click.option('--silent', is_flag=True)
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], callback=validate_region,
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def start_cluster(cluster_name, **kwargs):
    verbosity = 0 if kwargs['silent'] else 1
    group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    instance_ids = [instance.id for instance in group.instances()]
    if verbosity > 0:
        click.echo("Starting instances %s" % ', '.join(instance_ids))
    ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    ec2.start_instances(instance_ids=instance_ids)
    if verbosity > 0:
        click.echo("Waiting for started instances to become available...")
    wait_for_all_instances_reachable(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=kwargs['vpc_id'], verbosity=verbosity)
    if verbosity > 0:
        click.echo("Waiting for Myria service to become available...")
    if not wait_for_all_workers_online(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=kwargs['vpc_id'], verbosity=verbosity):
        print("""
The Myria service on your cluster '{cluster_name}' in the AWS '{region}' region returned an error.
Please refer to the error message above for diagnosis.
""".format(cluster_name=cluster_name, region=kwargs['region']))
        sys.exit(1)

    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if kwargs['vpc_id']:
        options_str += " --vpc-id %s" % kwargs['vpc_id']
    coordinator_public_hostname = get_coordinator_public_hostname(
        cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    print("""
Your Myria cluster '{cluster_name}' in the '{region}' region has been successfully restarted.
The public hostnames of all nodes in this cluster have changed.
You can view the new values by running

{script_name} list {cluster_name} {options}

New public hostname of coordinator:
{coordinator_public_hostname}
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str,
    coordinator_public_hostname=coordinator_public_hostname))


@run.command('update')
@click.argument('cluster_name')
@click.option('--silent', is_flag=True)
@click.option('--verbose', is_flag=True)
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], callback=validate_region,
    help="AWS region your cluster was launched in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
@click.option('--key-pair', show_default=True, default=DEFAULTS['key_pair'],
    help="EC2 key pair used to launch AMI builder instance")
@click.option('--private-key-file', callback=default_key_file_from_key_pair,
    help="Private key file for your EC2 key pair [default: %s]" % ("%s/.ssh/%s-myria_%s.pem" % (HOME, USER, DEFAULTS['region'])))
def update_cluster(cluster_name, **kwargs):
    verbosity = 3 if kwargs['verbose'] else 0 if kwargs['silent'] else 1

    validate_aws_settings(kwargs['region'], kwargs['profile'], kwargs['vpc_id'], verbosity=verbosity)
    try:
        get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    except ValueError:
        click.echo("No cluster with name '%s' exists in region '%s'." % (cluster_name, kwargs['region']))
        sys.exit(1)

    # generate Ansible EC2 dynamic inventory file
    ec2_ini_file = write_inventory_file(cluster_name, kwargs['region'], kwargs['profile'], verbosity=verbosity)

    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(CLUSTER_NAME=cluster_name)
    extra_vars.update(EC2_INI_PATH=ec2_ini_file)

    if verbosity > 1:
        for k, v in extra_vars.iteritems():
            click.echo("%s: %s" % (k, v))

    # run remote playbook to update software on EC2 instances
    click.echo("Updating Myria software on coordinator...")
    run_playbook("remote.yml", kwargs['private_key_file'], local=False, extra_vars=extra_vars,
        tags=['update'], destroy_cluster_on_failure=False, verbosity=verbosity)

    click.echo("Myria software successfully updated.")


def validate_list_options(ctx, param, value):
    if value is True:
        if ctx.params.get('coordinator') or ctx.params.get('workers'):
            raise click.BadParameter("Cannot specify both --coordinator and --workers")
        if not ctx.params.get('cluster_name'):
            raise click.BadParameter("Cluster name required with --coordinator or --workers")
    return value


@run.command('list')
@click.argument('cluster_name', required=False, is_eager=True)
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], callback=validate_region,
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
@click.option('--coordinator', is_flag=True, callback=validate_list_options,
    help="Output public DNS name of coordinator node")
@click.option('--workers', is_flag=True, callback=validate_list_options,
    help="Output public DNS names of worker nodes")
def list_cluster(cluster_name, **kwargs):
    if cluster_name is not None:
        if kwargs['coordinator']:
            print(get_coordinator_public_hostname(
                cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id']))
        elif kwargs['workers']:
            print('\n'.join(get_worker_public_hostnames(
                cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])))
        else:
            group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
            format_str = "{: <10} {: <50}"
            print(format_str.format('WORKER_IDS', 'HOST'))
            print(format_str.format('----------', '----'))
            instances = sorted(group.instances(), key=lambda i: int(i.tags.get('worker-id').split(',')[0]))
            for instance in instances:
                print(format_str.format(instance.tags.get('worker-id'), instance.public_dns_name))
    else:
        ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
        myria_groups = ec2.get_all_security_groups(filters={'tag:app': "myria"})
        groups = myria_groups
        if kwargs['vpc_id']:
            groups_in_vpc = ec2.get_all_security_groups(filters={'vpc-id': kwargs['vpc_id']})
            groups_in_vpc_ids = [g.id for g in groups_in_vpc]
            # In the EC2 API, filters can only express OR,
            # so we have to implement AND by intersecting results for each filter.
            groups = [g for g in myria_groups if g.id in groups_in_vpc_ids]
        format_str = "{: <20} {: <5} {: <50}"
        print(format_str.format('CLUSTER', 'NODES', 'COORDINATOR'))
        print(format_str.format('-------', '-----', '-----------'))
        for group in groups:
            coordinator = get_coordinator_public_hostname(
                group.name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
            print(format_str.format(group.name, len(group.instances()), coordinator))


def default_base_ami_id_from_region(ctx, param, value):
    if value is None:
        ami_id = None
        instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
        if instance_type_family in PV_INSTANCE_TYPE_FAMILIES:
            ami_id = DEFAULT_STOCK_PV_AMI_IDS.get(ctx.params['region'])
        else:
            ami_id = DEFAULT_STOCK_HVM_AMI_IDS.get(ctx.params['region'])
        if ami_id is None:
            raise click.BadParameter("No default AMI found for instance type '%s' in region '%s'" % (
                ctx.params['instance_type'], ctx.params['region']))
        return ami_id
    else:
        ctx.params['explicit_base_ami_id'] = True
        if ctx.params.get('virt_type') is not None:
            raise click.BadParameter("Cannot specify --%s if --base-ami-id is specified" % ctx.params['virt_type'])
        return value


def validate_virt_type(ctx, param, value):
    if value is not None:
        if ctx.params.get('explicit_base_ami_id'):
            raise click.BadParameter("Cannot specify --%s if --base-ami-id is specified" % value)
        if value == 'hvm':
            instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
            if instance_type_family in PV_INSTANCE_TYPE_FAMILIES:
                raise click.BadParameter("Instance type %s is incompatible with HVM virtualization" % ctx.params['instance_type'])
            ctx.params['base_ami_id'] = DEFAULT_STOCK_HVM_AMI_IDS[ctx.params['region']]
        elif value == 'pv':
            instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
            if instance_type_family not in PV_INSTANCE_TYPE_FAMILIES:
                raise click.BadParameter("Instance type %s is incompatible with PV virtualization" % ctx.params['instance_type'])
            ctx.params['base_ami_id'] = DEFAULT_STOCK_PV_AMI_IDS[ctx.params['region']]
    return value


def validate_regions(ctx, param, value):
    if value is not None:
        for region in value:
            if region not in ALL_REGIONS:
                raise click.BadParameter("Region must be one of the following:\n%s" % '\n'.join(ALL_REGIONS))
    return value


def wait_until_image_available(ami_id, region, profile=None, verbosity=0):
    ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
    image = ec2.get_image(ami_id)
    if verbosity > 0:
        click.echo("Waiting for AMI %s in region '%s' to become available..." % (ami_id, region))
    while image.state == 'pending':
        sleep(5)
        image.update()
    if image.state == 'available':
        return True
    else:
        if verbosity > 0:
            click.echo("Unexpected image status '%s' for AMI %s in region '%s'" % (image.state, ami_id, region))
        return False


@run.command('create-image')
@click.argument('ami_name')
@click.option('--verbose', is_flag=True, callback=validate_console_logging)
@click.option('--silent', is_flag=True, callback=validate_console_logging)
@click.option('--private', is_flag=True,
    help="Allow only this AWS account to use the new AMI to launch an EC2 instance")
@click.option('--overwrite', is_flag=True,
    help="Automatically deregister any existing AMI with the same name as new AMI")
@click.option('--force-terminate', is_flag=True,
    help="Automatically terminate any AMI builder instance with the same name as new AMI")
@click.option('--hvm', 'virt_type', flag_value='hvm', callback=validate_virt_type,
    help="Hardware Virtual Machine virtualization type (for current-generation EC2 instance types)")
@click.option('--pv', 'virt_type', flag_value='pv', callback=validate_virt_type,
    help="Paravirtual virtualization type (for previous-generation EC2 instance types)")
@click.option('--profile', default=None,
    help="Boto profile used to launch AMI builder instance")
@click.option('--key-pair', show_default=True, default=DEFAULTS['key_pair'],
    help="EC2 key pair used to launch AMI builder instance")
@click.option('--private-key-file', callback=default_key_file_from_key_pair,
    help="Private key file for your EC2 key pair [default: %s]" % ("%s/.ssh/%s-myria_%s.pem" % (HOME, USER, DEFAULTS['region'])))
@click.option('--instance-type', show_default=True, default=DEFAULTS['instance_type'], is_eager=True,
    help="EC2 instance type for AMI builder instance")
@click.option('--region', show_default=True, default=DEFAULTS['region'], is_eager=True, callback=validate_region,
    help="AWS region to launch AMI builder instance")
@click.option('--zone', show_default=True, default=None,
    help="AWS availability zone to launch AMI builder instance in")
@click.option('--subnet-id', default=None, callback=validate_subnet_id,
    help="ID of the VPC (Virtual Private Cloud) subnet used to launch AMI builder instance")
@click.option('--base-ami-id', callback=default_base_ami_id_from_region,
    help="ID of AMI (Amazon Machine Image) used to create new AMI [default: %s]" % DEFAULT_STOCK_HVM_AMI_IDS[DEFAULTS['region']])
@click.option('--description', default=None,
    help="Description of new AMI (\"Name\" in AWS console)")
@click.option('--copy-to-region', default=None, multiple=True, callback=validate_regions,
    help="Region to copy new AMI (can be specified multiple times)")
def create_image(ami_name, **kwargs):
    verbosity = 3 if kwargs['verbose'] else 0 if kwargs['silent'] else 1
    vpc_id = kwargs.get('vpc_id')
    iam_user = get_iam_user(kwargs['region'], profile=kwargs['profile'], verbosity=verbosity)
    ec2_ini_tmpfile = NamedTemporaryFile(delete=False)

    validate_aws_settings(kwargs['region'], kwargs['profile'], vpc_id, verbosity=verbosity)

    # abort or deregister if AMI with the same name already exists
    regions = kwargs['copy_to_region'] + (kwargs['region'],)
    for region in regions:
        ec2 = boto.ec2.connect_to_region(region, profile_name=kwargs['profile'])
        images = ec2.get_all_images(filters={'name': ami_name})
        if images:
            if kwargs['overwrite']:
                click.echo("Deregistering existing AMI with name '%s' (ID: %s) in region '%s'..." % (ami_name, images[0].id, region))
                images[0].deregister(delete_snapshot=True)
                # TODO: wait here for image to become unavailable, or we can hit a race at image creation
            else:
                click.echo("""
    AMI '{ami_name}' already exists in the '{region}' region.
    If you wish to create a new AMI with the same name,
    first deregister the existing AMI from the AWS console or
    run this command with the `--overwrite` option.
    """.format(ami_name=ami_name, region=region))
                sys.exit(1)

    # abort or delete group if group already exists
    instance_id = None
    group_id = None
    try:
        group = get_security_group_for_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except:
        pass
    else:
        group_id = group.id
        if kwargs['force_terminate']:
            click.echo("Destroying old AMI builder instance...")
            terminate_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        else:
            if group.instances():
                instance_id = group.instances()[0].id
            instance_str = "first terminate instance '{instance_id}' and then " if instance_id else ""
            click.echo("""
A builder instance for the AMI name '{ami_name}' already exists in the '{region}' region.
If you wish to create a new AMI with this name, please rerun this command with the `--force-terminate` switch or """ +
instance_str + """delete security group '{ami_name}' (ID: {group_id}) from the AWS console or AWS CLI.
""".format(ami_name=ami_name, region=kwargs['region'], group_id=group_id, instance_id=instance_id))
            sys.exit(1)

    # install keyboard interrupt handler to destroy partially-deployed cluster
    # TODO: signal handlers are inherited by each child process spawned by Ansible,
    # so messages are (harmlessly) duplicated for each process.
    def signal_handler(sig, frame):
        # ignore future interrupts
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        click.echo("User interrupted deployment, destroying instance...")
        try:
            terminate_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        except:
            pass # best-effort
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(AMI_NAME=ami_name)
    extra_vars.update(CLUSTER_NAME=ami_name)
    extra_vars.update(VPC_ID=vpc_id)
    extra_vars.update(EC2_INI_PATH=ec2_ini_tmpfile.name)
    if iam_user:
        extra_vars.update(IAM_USER=iam_user)

    if verbosity > 1:
        for k, v in extra_vars.iteritems():
            click.echo("%s: %s" % (k, v))

    # run local playbook to launch EC2 instances
    click.echo("Launching AMI builder instance...")
    run_playbook("launch-ami-builder.yml", kwargs['private_key_file'], extra_vars=extra_vars, max_retries=0, verbosity=verbosity)

    # run remote playbook to provision EC2 instances
    click.echo("Provisioning AMI builder instance...")
    run_playbook("remote.yml", kwargs['private_key_file'], local=False,
        extra_vars=extra_vars, tags=['provision'], verbosity=verbosity)

    click.echo("Bundling image...")
    try:
        image_ids_by_region = {}
        group = get_security_group_for_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        instance_id = group.instances()[0].id
        ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
        ami_id = ec2.create_image(instance_id=instance_id, name=ami_name, description=kwargs['description'])
        image_ids_by_region[kwargs['region']] = ami_id
        wait_until_image_available(ami_id, kwargs['region'], profile=kwargs['profile'], verbosity=verbosity)
        click.echo("Copying image to other regions...")
        for copy_region in kwargs['copy_to_region']:
            ec2 = boto.ec2.connect_to_region(copy_region, profile_name=kwargs['profile'])
            copy_image = ec2.copy_image(kwargs['region'], ami_id, name=ami_name, description=kwargs['description'])
            image_ids_by_region[copy_region] = copy_image.image_id
            wait_until_image_available(copy_image.image_id, copy_region, profile=kwargs['profile'], verbosity=verbosity)
        click.echo("Tagging images...")
        for region, ami_id in image_ids_by_region.iteritems():
            ec2 = boto.ec2.connect_to_region(region, profile_name=kwargs['profile'])
            image = ec2.get_image(ami_id)
            tags = {
                'Name': kwargs['description'],
                'base-image': kwargs['base_ami_id'],
                'app': "myria",
            }
            if iam_user:
                tags.update('user:Name', iam_user)
            image.add_tags(tags)
            if not kwargs['private']:
                # make AMI public
                image.set_launch_permissions(group_names='all')
    except Exception as e:
        if verbosity > 0:
            click.echo(e)
        click.echo("Unexpected error, destroying instance...")
        terminate_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        sys.exit(1)

    click.echo("Shutting down AMI builder instance...")
    try:
        terminate_cluster(ami_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except Exception as e:
        if verbosity > 0:
            click.echo(e)
        click.echo("Failed to properly shut down AMI builder instance. Please delete all instances in security group '%s'." % ami_name)

    click.echo("Successfully created images in regions %s:" % ', '.join(image_ids_by_region.keys()))
    format_str = "{: <20} {: <20}"
    print(format_str.format('REGION', 'AMI_ID'))
    print(format_str.format('------', '------'))
    for region, ami_id in image_ids_by_region.iteritems():
        print(format_str.format(region, ami_id))


def validate_vpc_ids(ctx, param, value):
    if value is not None:
        if len(value) != len(ctx.params['region']):
            raise click.BadParameter("--vpc-id must be specified as many times as --region if it is specified at all")
    return value


@run.command('delete-image')
@click.argument('ami_name')
@click.option('--profile', default=None,
    help="Boto profile used to create AMI")
@click.option('--region', multiple=True, is_eager=True, callback=validate_regions,
    help="Region in which AMI was created (can be specified multiple times)")
@click.option('--vpc-id', default=None, callback=validate_vpc_ids,
    help="ID of the VPC (Virtual Private Cloud) in which AMI was created (can be specified multiple times, in same order as regions)")
def delete_image(ami_name, **kwargs):
    regions = kwargs['region']
    for i, region in enumerate(regions):
        vpc_id = kwargs['vpc_id'][i] if kwargs['vpc_id'] else None
        validate_aws_settings(region, kwargs['profile'], vpc_id)
        ec2 = boto.ec2.connect_to_region(region, profile_name=kwargs['profile'])
         # In the EC2 API, filters can only express OR,
        # so we have to implement AND by intersecting results for each filter.
        if kwargs['vpc_id']:
            vpc_id = kwargs['vpc_id'][i]
            images_by_vpc = ec2.get_all_images(filters={'vpc-id': vpc_id})
            images = [img for img in images_by_vpc if img.name == ami_name]
        else:
            images = ec2.get_all_images(filters={'name': ami_name})
        if images:
            click.echo("Deregistering AMI with name '%s' (ID: %s) in region '%s'..." % (ami_name, images[0].id, region))
            images[0].deregister(delete_snapshot=True)
            # TODO: wait here for image to become unavailable
        else:
            click.echo("No AMI found in region '%s' with name '%s'" % (region, ami_name))


@run.command('list-images')
@click.option('--profile', default=None,
    help="Boto profile used to create AMI")
@click.option('--region', multiple=True, is_eager=True, callback=validate_regions,
    help="Region in which AMI was created (can be specified multiple times)")
@click.option('--vpc-id', default=None, callback=validate_vpc_ids,
    help="ID of the VPC (Virtual Private Cloud) in which AMI was created (can be specified multiple times, in same order as regions)")
def list_images(**kwargs):
    all_region_images = []
    regions = kwargs['region']
    for i, region in enumerate(regions):
        vpc_id = kwargs['vpc_id'][i] if kwargs['vpc_id'] else None
        validate_aws_settings(region, kwargs['profile'], vpc_id)
        ec2 = boto.ec2.connect_to_region(region, profile_name=kwargs['profile'])
        all_images = ec2.get_all_images(filters={'tag:app': "myria"})
        all_image_ids = [img.id for img in all_images]
        images = all_images
        if kwargs['vpc_id']:
            # In the EC2 API, filters can only express OR,
            # so we have to implement AND by intersecting results for each filter.
            images_in_vpc = ec2.get_all_images(filters={'vpc-id': kwargs['vpc_id']})
            images = [img for img in images_in_vpc if img.id in all_image_ids]
        all_region_images.extend(images)

    format_str = "{: <20} {: <20} {: <20} {: <30} {: <100}"
    print(format_str.format('REGION', 'AMI_ID', 'VIRTUALIZATION_TYPE', 'NAME', 'DESCRIPTION'))
    print(format_str.format('------', '------', '-------------------', '----', '-----------'))
    for image in all_region_images:
        print(format_str.format(image.region.name, image.id, image.virtualization_type, image.name, image.description))


# IMAGE ATTRIBUTES
# root_device_type
# ramdisk_id
# id
# owner_alias
# billing_products
# tags
# platform
# state
# location
# type
# virtualization_type
# sriov_net_support
# architecture
# description
# block_device_mapping
# kernel_id
# owner_id
# is_public
# instance_lifecycle
# creationDate
# name
# hypervisor
# region
# item
# connection
# root_device_name
# ownerId
# product_codes


if __name__ == '__main__':
    run()

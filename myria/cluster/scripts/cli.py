#!/usr/bin/env python

import sys
import os
import signal
from time import sleep
from tempfile import NamedTemporaryFile
from collections import namedtuple
import click
import yaml
import requests

import boto
import boto.ec2
import boto.vpc
import boto.iam
from boto.exception import EC2ResponseError

# Ansible configuration variables to set before importing Ansible modules
os.environ['ANSIBLE_SSH_ARGS'] = "-o ControlMaster=auto -o ControlPersist=60s -o UserKnownHostsFile=/dev/null"
os.environ['ANSIBLE_RECORD_HOST_KEYS'] = "False"
os.environ['ANSIBLE_HOST_KEY_CHECKING'] = "False"
os.environ['ANSIBLE_SSH_PIPELINING'] = "True"
os.environ['ANSIBLE_RETRY_FILES_ENABLED'] = "False"

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
ANSIBLE_GLOBAL_VARS_PATH = os.path.join(playbooks_dir, "group_vars/all")
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

PV_INSTANCE_TYPE_FAMILIES = ['c1', 'hi1', 'hs1', 'm1', 'm2', 't1']
LOCAL_STORAGE_INSTANCE_TYPE_FAMILIES = ['m1', 'm2', 'm3', 'c1', 'c3', 'r3', 'i2']

ANSIBLE_GLOBAL_VARS = yaml.load(file(ANSIBLE_GLOBAL_VARS_PATH, 'r'))

DEFAULTS = dict(
    key_pair="%s-myria" % USER,
    region='us-west-2',
    instance_type='t2.large',
    cluster_size=5,
    storage_type='ebs',
    data_volume_size_gb=ANSIBLE_GLOBAL_VARS['ebs_data_vol_volume_size'],
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

class Options(object):
    """
    Options class to replace Ansible OptParser
    """
    def __init__(self, subset=None, syntax=False, listhosts=False, listtasks=False, listtags=False,
                 tags=None, module_path=None, forks=MAX_CONCURRENT_TASKS, connection='smart',
                 remote_user=None, private_key_file=None, ssh_common_args=None, sftp_extra_args=None,
                 scp_extra_args=None, ssh_extra_args=None, become=False, become_method='sudo',
                 become_user='root', verbosity=0, check=False):
        self.subset = subset
        self.syntax = syntax
        self.listhosts = listhosts
        self.listtasks = listtasks
        self.listtags = listtags
        self.tags = tags
        self.module_path = module_path
        self.forks = forks
        self.connection = connection
        self.remote_user = remote_user
        self.private_key_file = private_key_file
        self.ssh_common_args = ssh_common_args
        self.sftp_extra_args = sftp_extra_args
        self.scp_extra_args = scp_extra_args
        self.ssh_extra_args = ssh_extra_args
        self.become = become
        self.become_method = become_method
        self.become_user = become_user
        self.verbosity = verbosity
        self.check = check


class Runner(object):

    def __init__(self, hostnames, playbook, private_key_file, run_data, tags=None, become_pass=None,
                 verbosity=0, callback=None, subset_pattern=None):

        self.hostnames = hostnames

        self.playbook = os.path.join(playbooks_dir, playbook)
        self.run_data = run_data

        self.options = Options(tags=tags, subset=subset_pattern, private_key_file=private_key_file, verbosity=verbosity)

        self.display = Display()
        self.display.verbosity = verbosity
        playbook_executor.verbosity = verbosity

        passwords = {'become_pass': None}

        # Gets data from YAML/JSON files
        self.loader = DataLoader()
        self.loader.set_vault_password(os.environ.get('VAULT_PASS'))

        self.variable_manager = VariableManager()
        self.variable_manager.extra_vars = self.run_data

        self.inventory = Inventory(loader=self.loader, variable_manager=self.variable_manager, host_list=self.hostnames)
        self.variable_manager.set_inventory(self.inventory)

        self.pbex = playbook_executor.PlaybookExecutor(
            playbooks=[self.playbook],
            inventory=self.inventory,
            variable_manager=self.variable_manager,
            loader=self.loader,
            options=self.options,
            passwords=passwords)

        if callback:
            self.pbex._tqm._stdout_callback = callback

    def run(self):
        self.pbex.run()
        stats = self.pbex._tqm._stats

        run_success = True
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            t = stats.summarize(h)
            if t['unreachable'] > 0 or t['failures'] > 0:
                run_success = False

        return run_success


class CallbackModule(CallbackBase):
    """
    Reference: https://github.com/ansible/ansible/blob/v2.0.0.2-1/lib/ansible/plugins/callback/default.py
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stored'
    CALLBACK_NAME = 'myria'

    def __init__(self, verbosity, retry_hosts):
        super(CallbackModule, self).__init__()
        self.verbosity = verbosity
        self.retry_hosts = retry_hosts

    def echo(self, msg):
        if self.verbosity > 0:
            click.echo(msg)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)

        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        # Catch an exception
        # This may never be called because default handler deletes
        # the exception, since Ansible thinks it knows better
        if 'exception' in result._result:
            # Extract the error message and log it
            # error = result._result['exception'].strip().split('\n')[-1]
            # print(error)
            msg = "An exception occurred during task execution. The full traceback is:\n" + result._result['exception']
            self.echo(msg)

            # Remove the exception from the result so it's not shown every time
            del result._result['exception']

        # Else log the reason for the failure
        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            if delegated_vars:
                self.echo("fatal: [%s -> %s]: FAILED! => %s" % (result._host.get_name(), delegated_vars['ansible_host'], self._dump_results(result._result)))
            else:
                self.echo("fatal: [%s]: FAILED! => %s" % (result._host.get_name(), self._dump_results(result._result)))

    def v2_runner_on_ok(self, result):
        self._clean_results(result._result, result._task.action)
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if result._task.action == 'include':
            return
        elif result._result.get('changed', False):
            if delegated_vars:
                msg = "changed: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "changed: [%s]" % result._host.get_name()
        else:
            if delegated_vars:
                msg = "ok: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "ok: [%s]" % result._host.get_name()

        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            self.echo(msg)

    def v2_runner_on_skipped(self, result):
        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            msg = "skipping: [%s]" % result._host.get_name()
            self.echo(msg)

    def v2_runner_on_unreachable(self, result):
        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if delegated_vars:
            self.echo("fatal: [%s -> %s]: UNREACHABLE! => %s" % (result._host.get_name(), delegated_vars['ansible_host'], self._dump_results(result._result)))
        else:
            self.echo("fatal: [%s]: UNREACHABLE! => %s" % (result._host.get_name(), self._dump_results(result._result)))

    def v2_runner_on_no_hosts(self, task):
        self.echo("skipping: no hosts matched")

    def v2_playbook_on_task_start(self, task, is_conditional):
        self.echo("TASK [%s]" % task.get_name().strip())

    def v2_playbook_on_play_start(self, play):
        name = play.get_name().strip()
        if not name:
            msg = "PLAY"
        else:
            msg = "PLAY [%s]" % name

        self.echo(msg)

    def v2_playbook_item_on_ok(self, result):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if result._task.action == 'include':
            return
        elif result._result.get('changed', False):
            if delegated_vars:
                msg = "changed: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "changed: [%s]" % result._host.get_name()
        else:
            if delegated_vars:
                msg = "ok: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "ok: [%s]" % result._host.get_name()

        msg += " => (item=%s)" % (result._result['item'])

        self.echo(msg)

    def v2_playbook_item_on_failed(self, result):
        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if 'exception' in result._result:
            msg = "An exception occurred during task execution. The full traceback is:\n" + result._result['exception']
            self.echo(msg)
            # Remove the exception from the result so it's not shown every time
            del result._result['exception']

        if delegated_vars:
            self.echo("failed: [%s -> %s] => (item=%s) => %s" % (result._host.get_name(), delegated_vars['ansible_host'], result._result['item'], self._dump_results(result._result)))
        else:
            self.echo("failed: [%s] => (item=%s) => %s" % (result._host.get_name(), result._result['item'], self._dump_results(result._result)))

    def v2_playbook_item_on_skipped(self, result):
        msg = "skipping: [%s] => (item=%s) " % (result._host.get_name(), result._result['item'])
        self.echo(msg)

    def v2_playbook_on_stats(self, stats):
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            t = stats.summarize(h)

            msg = "PLAY RECAP [%s] : %s %s %s %s %s" % (
                h,
                "ok: %s" % (t['ok']),
                "changed: %s" % (t['changed']),
                "unreachable: %s" % (t['unreachable']),
                "skipped: %s" % (t['skipped']),
                "failed: %s" % (t['failures']),
            )

            self.echo(msg)


def get_security_group_for_cluster(cluster_name, region, profile=None, vpc_id=None):
    ec2 = boto.ec2.connect_to_region(region, profile_name=profile)
    groups = []
    if vpc_id:
        # In the EC2 API, filters can only express OR,
        # so we have to implement AND by intersecting results for each filter.
        groups_in_vpc = ec2.get_all_security_groups(filters={'vpc-id': vpc_id})
        groups = [g for g in groups_in_vpc if g.name == cluster_name]
    else:
        groups_with_name = ec2.get_all_security_groups(filters={'group-name': cluster_name})
        groups = groups_with_name
    if len(groups) == 0: # no groups found
        raise ValueError("No security groups found with name '%s'" % cluster_name)
    elif len(groups) > 1: # multiple groups found
        raise ValueError("Multiple security groups found with name '%s'" % cluster_name)
    return groups[0]


def terminate_cluster(cluster_name, region, profile=None, vpc_id=None):
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
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
                click.echo("Security group state still converging, retrying in 5 seconds...")
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


def validate_volume_size(ctx, param, value):
    if value is not None:
        if ctx.params.get('storage_type') == "local":
            raise click.BadParameter("Cannot specify volume size with --storage-type=local")
    elif ctx.params.get('storage_type') == "ebs":
        return DEFAULTS['data_volume_size_gb']
    return value


def validate_storage_type(ctx, param, value):
    if value == "local":
        instance_type_family = instance_type_family_from_instance_type(ctx.params['instance_type'])
        if instance_type_family not in LOCAL_STORAGE_INSTANCE_TYPE_FAMILIES:
            raise click.BadParameter("Instance type %s is incompatible with local storage" % ctx.params['instance_type'])
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


# If this is called with `local=False`, then the environment variable `EC2_INI_PATH`
# must be set to a valid instance of the template `myria/cluster/playbooks/ec2.ini.j2`.
def run_playbook(playbook, private_key_file, local=True, extra_vars={}, tags=[], max_retries=MAX_RETRIES_DEFAULT, destroy_cluster_on_failure=True, verbosity=0):
    extra_vars.update(ansible_python_interpreter='/usr/bin/env python')
    cluster_name = extra_vars['CLUSTER_NAME']
    region = extra_vars['REGION']
    profile = extra_vars.get('PROFILE')
    vpc_id = extra_vars.get('VPC_ID')
    playbook_args = dict(
        hostnames=['localhost'] if local else INVENTORY_SCRIPT_PATH,
        playbook=playbook,
        private_key_file=private_key_file,
        run_data=extra_vars,
        verbosity=verbosity,
        tags=tags
    )
    # TODO: exponential backoff for unreachable hosts?
    retries = 0
    retry_hosts_pattern = None
    while True:
        retry_hosts = set()
        playbook_args.update(callback=CallbackModule(verbosity, retry_hosts), subset_pattern=retry_hosts_pattern)
        try:
            success = Runner(**playbook_args).run()
        except Exception as e:
            if verbosity > 0:
                click.echo(e)
            if destroy_cluster_on_failure:
                click.echo("Unexpected error, destroying cluster...")
                terminate_cluster(cluster_name, region=region, profile=profile, vpc_id=vpc_id)
            else:
                click.echo("Unexpected error, exiting...")
            sys.exit(1)
        if not success:
            assert retry_hosts
            if retries < max_retries:
                retries += 1
                retry_hosts_pattern = ",".join(retry_hosts)
                click.echo("Retrying playbook run on hosts %s (%d of %d)" % (retry_hosts_pattern, retries, max_retries))
            else:
                if destroy_cluster_on_failure:
                    click.echo("Maximum retries (%d) exceeded, destroying cluster..." % max_retries)
                    terminate_cluster(cluster_name, region=region, profile=profile, vpc_id=vpc_id)
                else:
                    click.echo("Maximum retries (%d) exceeded, exiting..." % max_retries)
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
    help="Boto profile used to launch your cluster")
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
    help="Number of EC2 instances in your cluster")
@click.option('--ami-id', callback=default_ami_id_from_region,
    help="ID of the AMI (Amazon Machine Image) used for your EC2 instances [default: %s]" % DEFAULT_PROVISIONED_HVM_AMI_IDS[DEFAULTS['region']])
@click.option('--subnet-id', default=None, callback=validate_subnet_id,
    help="ID of the VPC subnet in which to launch your EC2 instances")
@click.option('--role', help="Name of an IAM role used to launch your EC2 instances")
@click.option('--spot-price', help="Price in dollars of the maximum bid for an EC2 spot instance request")
@click.option('--storage-type', show_default=True, callback=validate_storage_type,
    type=click.Choice(['ebs', 'local']), default=DEFAULTS['storage_type'])
@click.option('--data-volume-size-gb', callback=validate_volume_size,
    help="Size of each instance's EBS data volume (used by Hadoop and PostgreSQL) in GB [default: %s]" % DEFAULTS['data_volume_size_gb'])
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
@click.option('--cluster-log-level', show_default=True,
    type=click.Choice(['OFF', 'FATAL', 'ERROR', 'WARN', 'DEBUG', 'TRACE', 'ALL']), default=DEFAULTS['cluster_log_level'])
def create_cluster(cluster_name, **kwargs):
    verbosity = 3 if kwargs['verbose'] else 0 if kwargs['silent'] else 1
    ec2_ini_tmpfile = NamedTemporaryFile(delete=False)
    os.environ['EC2_INI_PATH'] = ec2_ini_tmpfile.name
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
    try:
        get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except:
        pass
    else:
        click.echo("""
Cluster '{cluster_name}' already exists in the '{region}' region. If you wish to create a new cluster with the same name, first run

{script_name} destroy {cluster_name} {options}
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))
        sys.exit(1)

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

    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(CLUSTER_NAME=cluster_name)
    extra_vars.update(VPC_ID=vpc_id)
    extra_vars.update(EC2_INI_PATH=ec2_ini_tmpfile.name)
    if iam_user:
        extra_vars.update(IAM_USER=iam_user)

    if verbosity > 1:
        for k, v in extra_vars.iteritems():
            click.echo("%s: %s" % (k, v))

    # run local playbook to launch EC2 instances
    run_playbook("local.yml", kwargs['private_key_file'], extra_vars=extra_vars, max_retries=0, verbosity=verbosity)

    # poll instances for status until all are reachable
    group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    instance_ids = [instance.id for instance in group.instances()]
    while True:
        ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
        statuses = ec2.get_all_instance_status(instance_ids=instance_ids)
        for status in statuses:
            if status.system_status.details['reachability'] != "passed":
                if verbosity > 0:
                    click.echo("Not all instances reachable, waiting 60 seconds...")
                sleep(60)
                break
        else:
            break

    click.echo("All instances running...")

    # run remote playbook to provision EC2 instances
    all_provisioned_ami_ids = DEFAULT_PROVISIONED_HVM_AMI_IDS.values() + DEFAULT_PROVISIONED_PV_AMI_IDS.values()
    tags = ['configure'] if kwargs['ami_id'] in all_provisioned_ami_ids else ['provision', 'configure']
    run_playbook("remote.yml", kwargs['private_key_file'], local=False, extra_vars=extra_vars, tags=tags, verbosity=verbosity)

    # wait for all workers to become available
    if not wait_for_all_workers_online(cluster_name, kwargs['region'], profile=kwargs['profile'],
            vpc_id=vpc_id, verbosity=verbosity):
        print("""
The Myria service on your cluster '{cluster_name}' in the AWS '{region}' region returned an error.
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

Stop this cluster:
{script_name} stop {cluster_name} {options}

Start this cluster after stopping it:
{script_name} start {cluster_name} {options}

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
    instance_ids = [instance.id for instance in group.instances()]
    if verbosity > 0:
        click.echo("Stopping instances %s" % ', '.join(instance_ids))
    ec2 = boto.ec2.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    ec2.stop_instances(instance_ids=instance_ids)
    while True:
        for instance in group.instances():
            instance.update(validate=True)
            if instance.state != "stopped":
                if not kwargs['silent']:
                    click.echo("Instance %s not stopped, retrying in 30 seconds..." % instance.id)
                sleep(30)
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
    while True:
        for instance in group.instances():
            instance.update(validate=True)
            if instance.state != "running":
                if not kwargs['silent']:
                    click.echo("Instance %s not started, retrying in 30 seconds..." % instance.id)
                sleep(30)
                break # break out of for loop
        else: # all instances were started, so break out of while loop
            break

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
Your Myria cluster '{cluster_name}' in the AWS '{region}' region has been successfully restarted.
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
    ec2_ini_tmpfile = NamedTemporaryFile(delete=False)
    os.environ['EC2_INI_PATH'] = ec2_ini_tmpfile.name

    validate_aws_settings(kwargs['region'], kwargs['profile'], kwargs['vpc_id'], verbosity=verbosity)
    try:
        get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    except ValueError:
        click.echo("No cluster with name '%s' exists in region '%s'." % (cluster_name, kwargs['region']))
        sys.exit(1)

    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(CLUSTER_NAME=cluster_name)

    if verbosity > 1:
        for k, v in extra_vars.iteritems():
            click.echo("%s: %s" % (k, v))

    # generate Ansible EC2 dynamic inventory file
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(playbooks_dir))
    template = env.get_template("ec2.ini.j2")
    template_args = dict(REGION=kwargs['region'], CLUSTER_NAME=cluster_name)
    # We can't pass in None for a missing profile or the template won't behave correctly.
    if kwargs.get('profile'):
        template_args.update(PROFILE=kwargs['profile'])
    ec2_ini_tmpfile.write(template.render(template_args))
    # THIS IS CRITICAL (ec2.py won't see full file contents otherwise)
    ec2_ini_tmpfile.flush()

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
    os.environ['EC2_INI_PATH'] = ec2_ini_tmpfile.name

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
        ec2 = boto.ec2.connect_to_region(kwargs['region'])
        ami_id = ec2.create_image(instance_id=instance_id, name=ami_name, description=kwargs['description'])
        image_ids_by_region[kwargs['region']] = ami_id
        wait_until_image_available(ami_id, kwargs['region'], profile=kwargs['profile'], verbosity=verbosity)
        click.echo("Copying image to other regions...")
        for copy_region in kwargs['copy_to_region']:
            ec2 = boto.ec2.connect_to_region(copy_region)
            copy_image = ec2.copy_image(kwargs['region'], ami_id, name=ami_name, description=kwargs['description'])
            image_ids_by_region[copy_region] = copy_image.image_id
            wait_until_image_available(copy_image.image_id, copy_region, profile=kwargs['profile'], verbosity=verbosity)
        click.echo("Tagging images...")
        for region, ami_id in image_ids_by_region.iteritems():
            ec2 = boto.ec2.connect_to_region(region)
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

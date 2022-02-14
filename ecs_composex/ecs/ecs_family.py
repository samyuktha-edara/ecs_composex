#   -*- coding: utf-8 -*-
# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2022 John Mille <john@compose-x.io>

import re
from copy import deepcopy
from json import dumps
from os import path

from compose_x_common.compose_x_common import keyisset, keypresent, set_else_none
from troposphere import (
    AWS_ACCOUNT_ID,
    AWS_NO_VALUE,
    AWS_PARTITION,
    AWS_REGION,
    AWS_STACK_NAME,
    FindInMap,
    GetAtt,
    If,
    Join,
)
from troposphere import Output as CfnOutput
from troposphere import Ref, Sub, Tags
from troposphere.ec2 import SecurityGroup
from troposphere.ecs import (
    CapacityProviderStrategyItem,
    DockerVolumeConfiguration,
    EnvironmentFile,
    EphemeralStorage,
    LinuxParameters,
    MountPoint,
    RepositoryCredentials,
)
from troposphere.ecs import Service as EcsService
from troposphere.ecs import TaskDefinition, Volume
from troposphere.iam import Policy, PolicyType
from troposphere.logs import LogGroup

from ecs_composex.common import (
    FILE_PREFIX,
    add_outputs,
    add_parameters,
    build_template,
    setup_logging,
)
from ecs_composex.common.cfn_conditions import define_stack_name
from ecs_composex.common.cfn_params import Parameter
from ecs_composex.common.files import upload_file
from ecs_composex.common.services_helpers import set_logging_expiry
from ecs_composex.compose.compose_services import ComposeService
from ecs_composex.ecs import ecs_conditions, ecs_params
from ecs_composex.ecs.docker_tools import find_closest_fargate_configuration
from ecs_composex.ecs.ecs_iam import EcsRole
from ecs_composex.ecs.ecs_params import (
    AWS_XRAY_IMAGE,
    EXEC_ROLE_T,
    SERVICE_NAME,
    SG_T,
    TASK_ROLE_T,
    TASK_T,
)
from ecs_composex.ecs.ecs_predefined_alarms import PREDEFINED_SERVICE_ALARMS_DEFINITION
from ecs_composex.iam import add_role_boundaries, define_iam_policy
from ecs_composex.vpc.vpc_params import APP_SUBNETS, VPC_ID

LOG = setup_logging()


def handle_same_task_services_dependencies(services_config):
    """
    Function to define inter-tasks dependencies

    :param list services_config:
    :return:
    """
    for service in services_config:
        LOG.debug(service[1].depends_on)
        LOG.debug(
            any(
                k in [j[1].name for j in services_config] for k in service[1].depends_on
            )
        )
        if service[1].depends_on and any(
            k in [j[1].name for j in services_config] for k in service[1].depends_on
        ):
            service[1].container_definition.Essential = False
            parents = [
                s_service[1]
                for s_service in services_config
                if s_service[1].name in service[1].depends_on
            ]
            parents_dependency = [
                {
                    "ContainerName": p.name,
                    "Condition": p.container_start_condition,
                }
                for p in parents
            ]
            setattr(service[1].container_definition, "DependsOn", parents_dependency)
            for _ in parents:
                service[0] += 1


def assign_policy_to_role(role_secrets, role):
    """
    Function to assign the policy to role Policies
    :param list role_secrets:
    :param troposphere.iam.Role role:
    :return:
    """

    secrets_list = [secret.iam_arn for secret in role_secrets]
    secrets_kms_keys = [secret.kms_key_arn for secret in role_secrets if secret.kms_key]
    secrets_statement = {
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue"],
        "Sid": "AllowSecretsAccess",
        "Resource": [secret for secret in secrets_list],
    }
    secrets_keys_statement = {}
    if secrets_kms_keys:
        secrets_keys_statement = {
            "Effect": "Allow",
            "Action": ["kms:Decrypt"],
            "Sid": "AllowSecretsKmsKeyDecrypt",
            "Resource": [kms_key for kms_key in secrets_kms_keys],
        }
    role_policy = Policy(
        PolicyName="AccessToPreDefinedSecrets",
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [secrets_statement],
        },
    )
    if secrets_keys_statement:
        role_policy.PolicyDocument["Statement"].append(secrets_keys_statement)

    if hasattr(role, "Policies") and isinstance(role.Policies, list):
        existing_policy_names = [
            policy.PolicyName for policy in getattr(role, "Policies")
        ]
        if role_policy.PolicyName not in existing_policy_names:
            role.Policies.append(role_policy)
    else:
        setattr(role, "Policies", [role_policy])


def assign_secrets_to_roles(secrets, exec_role, task_role):
    """
    Function to assign secrets access policies to exec_role and/or task_role

    :param secrets:
    :param exec_role:
    :param task_role:
    :return:
    """
    exec_role_secrets = [secret for secret in secrets if EXEC_ROLE_T in secret.links]
    task_role_secrets = [secret for secret in secrets if TASK_ROLE_T in secret.links]
    LOG.debug(exec_role_secrets)
    LOG.debug(task_role_secrets)
    for secret in secrets:
        if EXEC_ROLE_T not in secret.links:
            LOG.warning(
                f"You did not specify {EXEC_ROLE_T} in your LinksTo for this secret. You will not have ECS"
                "Expose the value of the secret to your container."
            )
    if exec_role_secrets:
        assign_policy_to_role(exec_role_secrets, exec_role)
    if task_role_secrets:
        assign_policy_to_role(task_role_secrets, task_role)


def add_policies(config, key, new_policies):
    """
    Add IAM Policies from x-iam.Policies to the IAM TaskRole

    :param config:
    :param key:
    :param new_policies:
    :return:
    """
    existing_policies = config[key]
    existing_policy_names = [policy.PolicyName for policy in existing_policies]
    for count, policy in enumerate(new_policies):
        generated_name = (
            f"PolicyGenerated{count}"
            if f"PolicyGenerated{count}" not in existing_policy_names
            else f"PolicyGenerated{count + len(existing_policy_names)}"
        )
        name = (
            generated_name
            if not keyisset("PolicyName", policy)
            else policy["PolicyName"]
        )
        if name in existing_policy_names:
            return
        policy_object = Policy(PolicyName=name, PolicyDocument=policy["PolicyDocument"])
        existing_policies.append(policy_object)


def handle_iam_boundary(config, key, new_value):
    """

    :param config: the IAM Config
    :param key: The key, here, boundary
    :param new_value:

    """
    config[key] = define_iam_policy(new_value)


def identify_repo_credentials_secret(settings, task, secret_name):
    """
    Function to identify the secret_arn

    :param settings:
    :param ComposeFamily task:
    :param secret_name:
    :return:
    """
    for secret in settings.secrets:
        if secret.name == secret_name:
            secret_arn = secret.arn
            if secret_name not in [s.name for s in settings.secrets]:
                raise KeyError(
                    f"secret {secret_name} was not found in the defined secrets",
                    [s.name for s in settings.secrets],
                )
            if (
                secret.kms_key_arn
                and task.template
                and "RepositoryCredsKmsKeyAccess" not in task.template.resources
            ):
                task.template.add_resource(
                    PolicyType(
                        "RepositoryCredsKmsKeyAccess",
                        PolicyName="RepositoryCredsKmsKeyAccess",
                        PolicyDocument={
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": ["kms:Decrypt"],
                                    "Resource": [secret.kms_key_arn],
                                }
                            ],
                        },
                        Roles=[task.exec_role.name],
                    )
                )
            return secret_arn
    return None


def set_ecs_cluster_logging_s3_access(settings, policy, role_stack):
    """
    Based on ECS Cluster settings / configurations, grant permissions to put logs to S3 Bucket for logs defined to log
    ECS Execute command feature

    :param ecs_composex.common.settings.ComposeXSettings settings:
    :param policy:
    :param ecs_composex.common.stacks.ComposeXStack role_stack:
    """
    if settings.ecs_cluster.log_bucket:
        parameter = Parameter("EcsExecuteLoggingBucket", Type="String")
        add_parameters(role_stack.stack_template, [parameter])
        if isinstance(settings.ecs_cluster.log_bucket, FindInMap):
            role_stack.Parameters.update(
                {parameter.title: settings.ecs_cluster.log_bucket}
            )
        else:
            role_stack.Parameters.update(
                {parameter.title: Ref(settings.ecs_cluster.log_bucket.cfn_resource)}
            )
        policy.PolicyDocument["Statement"].append(
            {
                "Sid": "AllowDescribeS3Bucket",
                "Action": ["s3:GetEncryptionConfiguration"],
                "Resource": [
                    Sub(f"arn:${{{AWS_PARTITION}}}:s3:::${{{parameter.title}}}")
                ],
                "Effect": "Allow",
            }
        )
        policy.PolicyDocument["Statement"].append(
            {
                "Sid": "AllowS3BucketObjectWrite",
                "Action": ["s3:PutObject"],
                "Resource": [
                    Sub(f"arn:${{{AWS_PARTITION}}}:s3:::${{{parameter.title}}}/*")
                ],
                "Effect": "Allow",
            }
        )


def set_ecs_cluster_logging_kms_access(settings, policy, role_stack):
    """
    Based on ECS Cluster settings / configurations, grant permissions to KMS key encrypting Log defined to log
    ECS Execute command feature

    :param ecs_composex.common.settings.ComposeXSettings settings:
    :param policy:
    :param ecs_composex.common.stacks.ComposeXStack role_stack:
    """
    if settings.ecs_cluster.log_key:
        parameter = Parameter("EcsExecuteLoggingEncryptionKey", Type="String")
        add_parameters(role_stack.stack_template, [parameter])
        if isinstance(settings.ecs_cluster.log_key, FindInMap):
            role_stack.Parameters.update(
                {parameter.title: settings.ecs_cluster.log_key}
            )
        else:
            role_stack.Parameters.update(
                {
                    parameter.title: GetAtt(
                        settings.ecs_cluster.log_key.cfn_resource, "Arn"
                    )
                }
            )
        policy.PolicyDocument["Statement"].append(
            {
                "Action": [
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                "Resource": [Ref(parameter)],
                "Effect": "Allow",
            }
        )


def set_ecs_cluster_logging_cw_access(settings, policy, role_stack):
    """
    Based on ECS Cluster settings / configurations, grant permissions to CW Log defined to log
    ECS Execute command feature

    :param ecs_composex.common.settings.ComposeXSettings settings:
    :param policy:
    :param ecs_composex.common.stacks.ComposeXStack role_stack:
    """
    if settings.ecs_cluster.log_group:
        parameter = Parameter("EcsExecuteLoggingGroup", Type="String")
        add_parameters(role_stack.stack_template, [parameter])
        if isinstance(settings.ecs_cluster.log_group, FindInMap):
            role_stack.Parameters.update(
                {parameter.title: settings.ecs_cluster.log_group}
            )
            arn_value = Sub(
                f"arn:${{{AWS_PARTITION}}}:logs:${{{AWS_REGION}}}:"
                f"${{{AWS_ACCOUNT_ID}}}:${{{parameter.title}}}:*"
            )
        else:
            role_stack.Parameters.update(
                {parameter.title: GetAtt(settings.ecs_cluster.log_group, "Arn")}
            )
            arn_value = Ref(parameter)
        policy.PolicyDocument["Statement"].append(
            {
                "Sid": "AllowDescribingAllCWLogGroupsForSSMClient",
                "Action": ["logs:DescribeLogGroups"],
                "Resource": ["*"],
                "Effect": "Allow",
            }
        )
        policy.PolicyDocument["Statement"].append(
            {
                "Action": [
                    "logs:CreateLogStream",
                    "logs:DescribeLogStreams",
                    "logs:PutLogEvents",
                ],
                "Resource": [arn_value],
                "Effect": "Allow",
            }
        )


def set_ecs_cluster_logging_access(settings, policy, role_stack):
    """
    Based on ECS Cluster settings / configurations, grant permissions to specific resources
    for all functionalities to work.

    :param ecs_composex.common.settings.ComposeXSettings settings:
    :param policy:
    :param ecs_composex.common.stacks.ComposeXStack role_stack:
    """
    set_ecs_cluster_logging_kms_access(settings, policy, role_stack)
    set_ecs_cluster_logging_cw_access(settings, policy, role_stack)
    set_ecs_cluster_logging_s3_access(settings, policy, role_stack)


class ComposeFamily(object):
    """
    Class to group services logically to create the final ECS Service

    :ivar list[ecs_composex.compose.compose_services.ComposeService] services: List of the Services part of the family
    :ivar ecs_composex.ecs.ecs_service.Service ecs_service: ECS Service settings
    """

    default_launch_type = "EC2"

    def __init__(self, services, family_name):
        self.services = services
        self.ordered_services = []
        self.ignored_services = []
        self.name = family_name
        self.logical_name = re.sub(r"[^a-zA-Z0-9]+", "", family_name)
        self.iam = {
            "PermissionsBoundary": None,
            "ManagedPolicyArns": [],
            "Policies": [],
        }
        self.iam_modules_policies = {}
        self.family_hostname = self.name.replace("_", "-").lower()
        self.services_depends_on = []
        self.deployment_config = {}
        self.template = None
        self.use_xray = None
        self.stack = None
        self.task_definition = None
        self.service_definition = None
        self.service_tags = None
        self.task_ephemeral_storage = 0
        self.family_network_mode = None
        self.exec_role = EcsRole(self, ecs_params.EXEC_ROLE_T)
        self.task_role = EcsRole(self, ecs_params.TASK_ROLE_T)
        self.enable_execute_command = False
        self.scalable_target = None
        self.ecs_service = None
        self.launch_type = self.default_launch_type
        self.outputs = []

        self.set_template()
        self.set_family_launch_type()
        self.ecs_capacity_providers = []
        self.target_groups = []
        self.set_service_labels()
        self.set_compute_platform()
        self.task_logging_options = {}
        self.stack_parameters = {}
        self.alarms = {}
        self.predefined_alarms = {}
        self.set_initial_services_dependencies()
        self.sort_container_configs()
        self.handle_iam()
        self.add_containers_images_cfn_parameters()

    def generate_outputs(self):
        """
        Generates a list of CFN outputs for the ECS Service and Task Definition
        """
        self.outputs.append(
            CfnOutput(
                f"{self.logical_name}GroupId",
                Value=GetAtt(self.ecs_service.network.security_group, "GroupId"),
            )
        )
        self.outputs.append(
            CfnOutput(ecs_params.TASK_T, Value=Ref(self.task_definition))
        )
        self.outputs.append(
            CfnOutput(
                APP_SUBNETS.title,
                Value=Join(",", Ref(APP_SUBNETS)),
            )
        )
        if (
            self.scalable_target
            and self.scalable_target.title in self.template.resources
        ):
            self.outputs.append(
                CfnOutput(self.scalable_target.title, Value=Ref(self.scalable_target))
            )
        add_outputs(self.template, self.outputs)

    def set_template(self):
        """
        Function to set the tropopshere.Template associated with the ECS Service Family
        """
        self.template = build_template(
            f"Template for {self.name}",
            [
                ecs_params.CLUSTER_NAME,
                ecs_params.LAUNCH_TYPE,
                ecs_params.ECS_CONTROLLER,
                ecs_params.SERVICE_COUNT,
                ecs_params.CLUSTER_SG_ID,
                ecs_params.SERVICE_HOSTNAME,
                ecs_params.FARGATE_CPU_RAM_CONFIG,
                ecs_params.SERVICE_NAME,
                ecs_params.ELB_GRACE_PERIOD,
                ecs_params.FARGATE_VERSION,
                ecs_params.LOG_GROUP_RETENTION,
            ],
        )
        self.template.add_condition(
            ecs_conditions.SERVICE_COUNT_ZERO_CON_T,
            ecs_conditions.SERVICE_COUNT_ZERO_CON,
        )
        self.template.add_condition(
            ecs_conditions.SERVICE_COUNT_ZERO_AND_FARGATE_CON_T,
            ecs_conditions.SERVICE_COUNT_ZERO_AND_FARGATE_CON,
        )
        self.template.add_condition(
            ecs_conditions.USE_HOSTNAME_CON_T, ecs_conditions.USE_HOSTNAME_CON
        )
        self.template.add_condition(
            ecs_conditions.NOT_USE_HOSTNAME_CON_T,
            ecs_conditions.NOT_USE_HOSTNAME_CON,
        )
        self.template.add_condition(
            ecs_conditions.NOT_USE_CLUSTER_SG_CON_T,
            ecs_conditions.NOT_USE_CLUSTER_SG_CON,
        )
        self.template.add_condition(
            ecs_conditions.USE_CLUSTER_SG_CON_T, ecs_conditions.USE_CLUSTER_SG_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_FARGATE_PROVIDERS_CON_T,
            ecs_conditions.USE_FARGATE_PROVIDERS_CON,
        )
        self.template.add_condition(
            ecs_conditions.USE_FARGATE_LT_CON_T, ecs_conditions.USE_FARGATE_LT_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_FARGATE_CON_T,
            ecs_conditions.USE_FARGATE_CON,
        )
        self.template.add_condition(
            ecs_conditions.NOT_FARGATE_CON_T, ecs_conditions.NOT_FARGATE_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_EC2_CON_T, ecs_conditions.USE_EC2_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_SERVICE_MODE_CON_T, ecs_conditions.USE_SERVICE_MODE_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_CLUSTER_MODE_CON_T, ecs_conditions.USE_CLUSTER_MODE_CON
        )
        self.template.add_condition(
            ecs_conditions.USE_EXTERNAL_LT_T, ecs_conditions.USE_EXTERNAL_LT
        )
        self.template.add_condition(
            ecs_conditions.USE_LAUNCH_TYPE_CON_T, ecs_conditions.USE_LAUNCH_TYPE_CON
        )

    def state_facts(self):
        """
        Function to display facts about the family.
        """
        LOG.info(f"{self.name} - Hostname set to {self.family_hostname}")
        LOG.info(f"{self.name} - Ephemeral storage: {self.task_ephemeral_storage}")
        LOG.info(f"{self.name} - LaunchType set to {self.launch_type}")
        LOG.info(
            f"{self.name} - TaskDefinition containers: {[svc.name for svc in self.services]}"
        )

    def set_family_launch_type(self):
        """
        Goes over all the services and verifies if one of them is set to use EXTERNAL mode.
        If so, overrides for all
        """
        if self.launch_type == "EXTERNAL":
            LOG.debug(f"{self.name} is already set to EXTERNAL")
        for service in self.services:
            if service.launch_type == "EXTERNAL":
                LOG.info(
                    f"{self.name} - service {service.name} is set to EXTERNAL. Overriding for all"
                )
                self.launch_type = "EXTERNAL"
                break

    def add_security_group(self):
        """
        Creates a new EC2 SecurityGroup and assigns to ecs_service.network_settings
        Adds the security group to the family template resources.
        """
        self.ecs_service.network.security_group = SecurityGroup(
            SG_T,
            GroupDescription=Sub(
                f"SG for ${{{SERVICE_NAME.title}}} - ${{STACK_NAME}}",
                STACK_NAME=define_stack_name(),
            ),
            Tags=Tags(
                {
                    "Name": Sub(
                        f"${{{SERVICE_NAME.title}}}-${{STACK_NAME}}",
                        STACK_NAME=define_stack_name(),
                    ),
                    "StackName": Ref(AWS_STACK_NAME),
                    "MicroserviceName": Ref(SERVICE_NAME),
                }
            ),
            VpcId=Ref(VPC_ID),
        )
        if (
            self.template
            and self.ecs_service.network.security_group.title
            not in self.template.resources
        ):
            self.template.add_resource(self.ecs_service.network.security_group)
        if (
            self.ecs_service.network.security_group
            not in self.ecs_service.security_groups
        ):
            self.ecs_service.security_groups.append(
                Ref(self.ecs_service.network.security_group)
            )

    def add_service(self, service):
        """
        Adds a new container/service to the Task Family and validates all settings that go along with the change.
        :param service:
        """
        if service.name in [svc.name for svc in self.services]:
            LOG.debug(
                f"{self.name} - container service {service.name} is already set. Skipping"
            )
            return
        self.services.append(service)
        if self.task_definition and service.container_definition:
            self.task_definition.ContainerDefinitions.append(
                service.container_definition
            )
            self.set_secrets_access()
        self.set_task_ephemeral_storage()
        self.refresh()

    def refresh(self):
        """
        Refresh the ComposeFamily settings as a result of a change
        """
        self.sort_container_configs()
        self.set_compute_platform()
        self.merge_capacity_providers()
        self.handle_iam()
        self.handle_logging()
        self.add_containers_images_cfn_parameters()
        self.set_task_compute_parameter()
        self.set_family_hostname()

    def finalize_family_settings(self):
        """
        Once all services have been added, we add the sidecars and deal with appropriate permissions and settings
        Will add xray / prometheus sidecars
        """
        self.set_xray()
        self.set_prometheus()
        if self.launch_type == "EXTERNAL":
            if hasattr(self.ecs_service.ecs_service, "LoadBalancers"):
                setattr(
                    self.ecs_service.ecs_service, "LoadBalancers", Ref(AWS_NO_VALUE)
                )
            if hasattr(self.ecs_service.ecs_service, "ServiceRegistries"):
                setattr(
                    self.ecs_service.ecs_service, "ServiceRegistries", Ref(AWS_NO_VALUE)
                )
            for container in self.task_definition.ContainerDefinitions:
                if hasattr(container, "LinuxParameters"):
                    parameters = getattr(container, "LinuxParameters")
                    setattr(parameters, "InitProcessEnabled", False)
        if (
            self.ecs_service.ecs_service
            and self.ecs_service.ecs_service.title in self.template.resources
        ) and (
            self.scalable_target
            and self.scalable_target.title not in self.template.resources
        ):
            self.template.add_resource(self.scalable_target)
        self.generate_outputs()

    def set_initial_services_dependencies(self):
        """
        Method to iterate over each depends_on service set in the family services and add them up

        :return:
        """
        for service in self.services:
            if service.depends_on:
                for service_depends_on in service.depends_on:
                    if service_depends_on not in self.services_depends_on:
                        self.services_depends_on.append(service_depends_on)

    def set_service_labels(self):
        """
        Sets default service tags and labels
        """
        default_tags = Tags(
            {
                "Name": Ref(ecs_params.SERVICE_NAME),
                "StackName": Ref(AWS_STACK_NAME),
                "compose-x::name": self.name,
                "compose-x::logical_name": self.logical_name,
            }
        )
        if not self.service_tags:
            self.service_tags = default_tags
        for svc in self.services:
            if not svc.deploy_labels:
                continue
            if isinstance(svc.deploy_labels, list):
                continue
            self.service_tags += Tags(**svc.deploy_labels)

    def set_task_ephemeral_storage(self):
        """
        If any service ephemeral storage is defined above, sets the ephemeral storage to the maximum of them.
        """
        max_storage = max([service.ephemeral_storage for service in self.services])
        if max_storage >= 21:
            self.task_ephemeral_storage = max_storage

    def set_compute_platform(self):
        """
        Iterates over all services and if ecs.compute.platform
        """
        if self.launch_type != self.default_launch_type:
            LOG.debug(
                f"{self.name} - The compute platform is already overridden to {self.launch_type}"
            )
            for service in self.services:
                setattr(service, "compute_platform", self.launch_type)
        elif not all(
            service.launch_type == self.launch_type for service in self.services
        ):
            for service in self.services:
                if service.launch_type != self.launch_type:
                    platform = service.launch_type
                    LOG.debug(
                        f"{self.name} - At least one service is defined not to be on FARGATE."
                        f" Overriding to {platform}"
                    )
                    self.launch_type = platform
        if self.stack:
            self.stack.Parameters.update(
                {ecs_params.LAUNCH_TYPE.title: self.launch_type}
            )

    def set_enable_execute_command(self):
        """
        Sets necessary settings to enable ECS Execute Command
        """
        if self.launch_type == "EXTERNAL":
            LOG.warning(
                f"{self.name} - ECS Execute Command is not supported for services running on ECS Anywhere"
            )
            return
        for svc in self.services:
            if svc.is_aws_sidecar:
                continue
            if svc.x_ecs and keyisset("EnableExecuteCommand", svc.x_ecs):
                self.enable_execute_command = True
        if (
            self.enable_execute_command
            and self.task_definition
            and self.task_definition.ContainerDefinitions
        ):
            for container in self.task_definition.ContainerDefinitions:
                if hasattr(container, "LinuxParameters"):
                    params = getattr(container, "LinuxParameters")
                    setattr(params, "InitProcessEnabled", True)
                else:
                    setattr(
                        container,
                        "LinuxParameters",
                        LinuxParameters(InitProcessEnabled=True),
                    )

    def apply_ecs_execute_command_permissions(self, settings):
        """
        Method to set the IAM Policies in place to allow ECS Execute SSM and Logging

        :param settings:
        :return:
        """
        policy_title = "EnableEcsExecuteCommand"
        role_stack = self.task_role.stack
        task_role = Ref(self.task_role.cfn_resource)
        if policy_title not in role_stack.stack_template.resources:
            policy = role_stack.stack_template.add_resource(
                PolicyType(
                    policy_title,
                    PolicyName="EnableExecuteCommand",
                    PolicyDocument={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": [
                                    "ssmmessages:CreateControlChannel",
                                    "ssmmessages:CreateDataChannel",
                                    "ssmmessages:OpenControlChannel",
                                    "ssmmessages:OpenDataChannel",
                                ],
                                "Resource": "*",
                            }
                        ],
                    },
                    Roles=[task_role],
                )
            )
            set_ecs_cluster_logging_access(settings, policy, role_stack)
        elif policy_title in role_stack.stack_template.resources:
            policy = role_stack.stack_template.resources[policy_title]
            if hasattr(policy, "Roles"):
                roles = getattr(policy, "Roles")
                if roles:
                    for role in roles:
                        if (
                            isinstance(role, Ref)
                            and role.data["Ref"] != task_role.data["Ref"]
                        ):
                            roles.append(task_role)
            else:
                setattr(policy, "Roles", [task_role])
        setattr(
            self.ecs_service.ecs_service,
            "EnableExecuteCommand",
            self.enable_execute_command,
        )

    def set_xray(self):
        """
        Automatically adds the xray-daemon sidecar to the task definition.

        Evaluates if any of the services x_ray is True to add.
        If any(True) then checks whether the xray-daemon container is already in the services.
        """
        self.use_xray = any([service.x_ray for service in self.services])
        if self.use_xray is False:
            return
        xray_service = None
        if "xray-daemon" not in [service.name for service in self.services]:
            xray_service = ComposeService(
                "xray-daemon",
                {
                    "image": AWS_XRAY_IMAGE,
                    "deploy": {
                        "resources": {"limits": {"cpus": 0.03125, "memory": "256M"}},
                    },
                    "x-iam": {
                        "ManagedPolicyArns": [
                            "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
                        ]
                    },
                },
            )
            xray_service.is_aws_sidecar = True
            self.add_service(xray_service)
            if xray_service.name not in self.ignored_services:
                self.ignored_services.append(xray_service)
        else:
            for service in self.services:
                if not service.name == "xray-daemon":
                    continue
                else:
                    xray_service = service
                    break
        if not xray_service:
            raise ValueError("xray_service is not set ?!")
        for service in self.services:
            if service.is_aws_sidecar:
                continue
            if xray_service.name not in service.depends_on:
                service.depends_on.append(xray_service.name)
                LOG.info(
                    f"{self.name} - Adding xray-daemon as dependency to {service.name}"
                )

    def define_predefined_alarm_settings(self, new_settings):
        """
        Method to define the predefined alarm settings based on the alarm characteristics

        :param new_settings:
        :return:
        """
        for alarm_name, alarm_def in new_settings["Alarms"].items():
            if not keyisset("Properties", alarm_def):
                continue
            props = alarm_def["Properties"]
            if not keyisset("MetricName", props):
                raise KeyError("You must define a MetricName for the pre-defined alarm")
            metric_name = props["MetricName"]
            if metric_name == "RunningTaskCount":
                range_key = "max"
                if keyisset("range_key", new_settings):
                    range_key = new_settings["range_key"]
                new_settings["Settings"][
                    metric_name
                ] = self.ecs_service.scaling.scaling_range[range_key]

    def define_predefined_alarms(self):
        """
        Method to define which predefined alarms are available
        :return: dict of the alarms
        :rtype: dict
        """

        finalized_alarms = {}
        for name, settings in PREDEFINED_SERVICE_ALARMS_DEFINITION.items():
            if (
                keyisset("requires_scaling", settings)
                and not self.ecs_service.scaling.defined
            ):
                LOG.debug(
                    f"{self.name} - No x-scaling.Range defined for the service and rule {name} requires it. Skipping"
                )
                continue
            new_settings = deepcopy(settings)
            self.define_predefined_alarm_settings(new_settings)
            finalized_alarms[name] = new_settings
        return finalized_alarms

    def validate_service_predefined_alarms(self, valid_predefined, service_predefined):
        """
        Validates that the alarms set to use exist

        :raises: KeyError if the name for Predefined alarm is not found in services alarms
        """
        if not all(
            name in valid_predefined.keys() for name in service_predefined.keys()
        ):
            raise KeyError(
                f"For {self.logical_name}, only valid service_predefined alarms are",
                valid_predefined.keys(),
                "Got",
                service_predefined.keys(),
            )

    def define_default_alarm_settings(self, key, value, settings_key, valid_predefined):
        if not keyisset(key, self.predefined_alarms):
            self.predefined_alarms[key] = valid_predefined[key]
            self.predefined_alarms[key][settings_key] = valid_predefined[key][
                settings_key
            ]
            if isinstance(value, dict) and keyisset(settings_key, value):
                self.predefined_alarms[key][settings_key] = valid_predefined[key][
                    settings_key
                ]
                for subkey, subvalue in value[settings_key].items():
                    self.predefined_alarms[key][settings_key][subkey] = subvalue

    def merge_alarm_settings(self, key, value, settings_key, valid_predefined):
        """
        Method to merge multiple services alarms definitions

        :param str key:
        :param dict value:
        :param str settings_key:
        :return:
        """
        for subkey, subvalue in value[settings_key].items():
            if isinstance(subvalue, (int, float)) and keyisset(
                subkey, self.predefined_alarms[key][settings_key]
            ):
                set_value = self.predefined_alarms[key][settings_key][subkey]
                new_value = subvalue
                LOG.warning(
                    f"{self.name} - Value for {key}.Settings.{subkey} override from {set_value} to {new_value}."
                )
                self.predefined_alarms[key]["Settings"][subkey] = new_value

    def set_merge_alarm_topics(self, key, value):
        topics = value["Topics"]
        set_topics = []
        if keyisset("Topics", self.predefined_alarms[key]):
            set_topics = self.predefined_alarms[key]["Topics"]
        else:
            self.predefined_alarms[key]["Topics"] = set_topics
        for topic in topics:
            if isinstance(topic, str) and topic not in [
                t for t in set_topics if isinstance(t, str)
            ]:
                set_topics.append(topic)
            elif (
                isinstance(topic, dict)
                and keyisset("x-sns", topic)
                and topic["x-sns"]
                not in [
                    t["x-sns"]
                    for t in set_topics
                    if isinstance(t, dict) and keyisset("x-sns", t)
                ]
            ):
                set_topics.append(topic)

    def assign_predefined_alerts(
        self, service_predefined, valid_predefined, settings_key
    ):
        for key, value in service_predefined.items():
            if not keyisset(key, self.predefined_alarms):
                self.define_default_alarm_settings(
                    key, value, settings_key, valid_predefined
                )
            elif (
                keyisset(key, self.predefined_alarms)
                and isinstance(value, dict)
                and keyisset(settings_key, value)
            ):
                self.merge_alarm_settings(key, value, settings_key, valid_predefined)
            if keyisset("Topics", value):
                self.set_merge_alarm_topics(key, value)

    def handle_alarms(self):
        """
        Method to define the alarms for the services.
        """
        valid_predefined = self.define_predefined_alarms()
        LOG.debug(self.logical_name, valid_predefined)
        if not valid_predefined:
            return
        alarm_key = "x-alarms"
        settings_key = "Settings"
        for service in self.services:
            if keyisset(alarm_key, service.definition) and keyisset(
                "Predefined", service.definition[alarm_key]
            ):
                service_predefined = service.definition[alarm_key]["Predefined"]
                self.validate_service_predefined_alarms(
                    valid_predefined, service_predefined
                )
                self.assign_predefined_alerts(
                    service_predefined, valid_predefined, settings_key
                )
                LOG.debug(self.predefined_alarms)

    def add_container_level_log_group(self, service, log_group_title, expiry):
        """
        Method to add a new log group for a specific container/service defined when awslogs-group has been set.

        :param service:
        :param str log_group_title:
        :param expiry:
        """
        if log_group_title not in self.template.resources:
            log_group = self.template.add_resource(
                LogGroup(
                    log_group_title,
                    LogGroupName=service.logging.Options["awslogs-group"],
                    RetentionInDays=expiry,
                )
            )
            policy = PolicyType(
                f"CloudWatchAccessFor{self.logical_name}{log_group_title}",
                PolicyName=f"CloudWatchAccessFor{self.logical_name}{log_group_title}",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowCloudWatchLoggingToSpecificLogGroup",
                            "Effect": "Allow",
                            "Action": [
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            "Resource": GetAtt(log_group, "Arn"),
                        }
                    ],
                },
                Roles=[self.exec_role.name],
            )
            if self.template and policy.title not in self.template.resources:
                self.template.add_resource(policy)
            service.logging.Options.update({"awslogs-group": Ref(log_group)})
        else:
            LOG.debug("LOG Group and policy already exist")

    def handle_logging(self):
        """
        Method to go over each service logging configuration and accordingly define the IAM permissions needed for
        the exec role
        """
        if not self.template:
            return
        for service in self.services:
            expiry = set_logging_expiry(service)
            log_group_title = f"{service.logical_name}LogGroup"
            if keyisset("awslogs-region", service.logging.Options) and not isinstance(
                service.logging.Options["awslogs-region"], Ref
            ):
                LOG.warning(
                    f"{self.name}.logging - When defining awslogs-region, Compose-X does not create the CW Log Group"
                )
                self.exec_role.cfn_resource.Policies.append(
                    Policy(
                        PolicyName=f"CloudWatchAccessFor{self.logical_name}",
                        PolicyDocument={
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AllowCloudWatchLoggingToSpecificLogGroup",
                                    "Effect": "Allow",
                                    "Action": [
                                        "logs:CreateLogStream",
                                        "logs:CreateLogGroup",
                                        "logs:PutLogEvents",
                                    ],
                                    "Resource": "*",
                                }
                            ],
                        },
                    )
                )
            elif keyisset("awslogs-group", service.logging.Options) and not isinstance(
                service.logging.Options["awslogs-group"], (Ref, Sub)
            ):
                self.add_container_level_log_group(service, log_group_title, expiry)
            else:
                service.logging.Options.update(
                    {"awslogs-group": Ref(ecs_params.LOG_GROUP_T)}
                )

    def sort_container_configs(self):
        """
        Method to sort out the containers dependencies and create the containers definitions based on the configs.
        :return:
        """
        service_configs = [[0, service] for service in self.services]
        handle_same_task_services_dependencies(service_configs)
        ordered_containers_config = sorted(service_configs, key=lambda i: i[0])
        self.ordered_services = [s[1] for s in ordered_containers_config]
        for service in self.ordered_services:
            if (
                service.container_start_condition == "SUCCESS"
                or service.container_start_condition == "COMPLETE"
                or service.is_aws_sidecar
                or not service.is_essential
            ):
                service.container_definition.Essential = False
            else:
                service.container_definition.Essential = True

        LOG.debug(service_configs, ordered_containers_config)
        LOG.debug(
            "Essentially",
            ordered_containers_config[0][1].name,
            ordered_containers_config[0][1].container_definition.Essential,
        )
        LOG.debug(
            dumps(
                [service.container_definition.to_dict() for service in self.services],
                indent=4,
            )
        )
        if len(ordered_containers_config) == 1:
            LOG.debug("There is only one service, we need to ensure it is essential")
            ordered_containers_config[0][1].container_definition.Essential = True

        for service in self.services:
            self.stack_parameters.update(service.container_parameters)

    def sort_iam_settings(self, key, setting):
        """
        Method to sort out iam configuration

        :param tuple key:
        :param dict setting:
        :return:
        """
        if keyisset(key[0], setting) and isinstance(setting[key[0]], key[1]):
            if key[2]:
                key[2](self.iam, key[0], setting[key[0]])
            else:
                if key[1] is list and keypresent(key[0], self.iam):
                    self.iam[key[0]] = list(set(self.iam[key[0]] + setting[key[0]]))
                if key[1] is str and keypresent(key[0], self.iam):
                    self.iam[key[0]] = setting[key[0]]

    def handle_iam(self):
        valid_keys = [
            ("ManagedPolicyArns", list, None),
            ("Policies", list, add_policies),
            ("PermissionsBoundary", (str, Sub), handle_iam_boundary),
        ]
        iam_settings = [service.x_iam for service in self.services if service.x_iam]
        for setting in iam_settings:
            for key in valid_keys:
                self.sort_iam_settings(key, setting)

        self.set_secrets_access()

    def handle_permission_boundary(self, prop_key):
        """
        Function to add the permissions boundary to IAM roles

        :param str prop_key:
        :return:
        """
        if keyisset(prop_key, self.iam):
            add_role_boundaries(self.exec_role.cfn_resource, self.iam[prop_key])
            add_role_boundaries(self.task_role.cfn_resource, self.iam[prop_key])

    def assign_iam_policies(self, role, prop):
        """
        Method to handle assignment of IAM policies defined from compose file.

        :param role:
        :param prop:
        :return:
        """
        if hasattr(role, prop[1]):
            existing = getattr(role, prop[1])
            existing_policy_names = [policy.PolicyName for policy in existing]
            for new_policy in self.iam[prop[0]]:
                if new_policy.PolicyName not in existing_policy_names:
                    existing.append(new_policy)
        else:
            setattr(role, prop[1], self.iam[prop[0]])

    def assign_iam_managed_policies(self, role, prop):
        """
        Method to assign managed policies to IAM role

        :param role:
        :param prop:
        :return:
        """
        if hasattr(role, prop[1]):
            setattr(
                role,
                prop[1],
                list(set(self.iam[prop[0]] + getattr(role, prop[1]))),
            )
        else:
            setattr(role, prop[1], self.iam[prop[0]])

    def assign_policies(self, role_name=None):
        """
        Method to assign IAM configuration (policies, boundary etc.) to the Task Role.
        Role can be overriden

        :param str role_name: The role LogicalName as defined in the template
        """
        if role_name is None:
            role_name = TASK_ROLE_T
        if role_name == EXEC_ROLE_T:
            role = self.exec_role.cfn_resource
        else:
            role = self.task_role.cfn_resource
        props = [
            (
                "ManagedPolicyArns",
                "ManagedPolicyArns",
                list,
                self.assign_iam_managed_policies,
            ),
            ("Policies", "Policies", list, self.assign_iam_policies),
            ("PermissionsBoundary", "PermissionsBoundary", (str, Sub), None),
        ]
        for prop in props:
            if keyisset(prop[0], self.iam) and isinstance(self.iam[prop[0]], prop[2]):
                if prop[0] == "PermissionsBoundary":
                    self.handle_permission_boundary(prop[0])
                elif prop[3]:
                    prop[3](role, prop)

    def set_secrets_access(self):
        """
        Method to handle secrets permissions access
        """
        if not self.exec_role or not self.task_role:
            return
        secrets = []
        for service in self.services:
            for secret in service.secrets:
                secrets.append(secret)
        if secrets:
            assign_secrets_to_roles(
                secrets,
                self.exec_role.cfn_resource,
                self.task_role.cfn_resource,
            )

    def set_task_compute_parameter(self):
        """
        Method to update task parameter for CPU/RAM profile
        """
        tasks_cpu = 0
        tasks_ram = 0
        for service in self.services:
            container = service.container_definition
            if isinstance(container.Cpu, int):
                tasks_cpu += container.Cpu
            if isinstance(container.Memory, int) and isinstance(
                container.MemoryReservation, int
            ):
                tasks_ram += max(container.Memory, container.MemoryReservation)
            elif isinstance(container.Memory, Ref) and isinstance(
                container.MemoryReservation, int
            ):
                tasks_ram += container.MemoryReservation
            elif isinstance(container.Memory, int) and isinstance(
                container.MemoryReservation, Ref
            ):
                tasks_ram += container.Memory
            else:
                LOG.debug(
                    f"{service.name} does not have RAM settings."
                    "Based on CPU, it will pick the smaller RAM Fargate supports"
                )
        if tasks_cpu > 0 or tasks_ram > 0:
            cpu_ram = find_closest_fargate_configuration(tasks_cpu, tasks_ram, True)
            LOG.debug(
                f"{self.logical_name} Task CPU: {tasks_cpu}, RAM: {tasks_ram} => {cpu_ram}"
            )
            self.stack_parameters.update({ecs_params.FARGATE_CPU_RAM_CONFIG_T: cpu_ram})

    def set_task_definition(self):
        """
        Function to set or update the task definition

        :param self: the self of services
        """
        self.task_definition = TaskDefinition(
            TASK_T,
            template=self.template,
            Cpu=ecs_params.FARGATE_CPU,
            Memory=ecs_params.FARGATE_RAM,
            NetworkMode=If(
                ecs_conditions.USE_FARGATE_CON_T,
                "awsvpc",
                Ref(AWS_NO_VALUE)
                if not self.family_network_mode
                else self.family_network_mode,
            ),
            EphemeralStorage=If(
                ecs_conditions.USE_FARGATE_CON_T,
                EphemeralStorage(SizeInGiB=self.task_ephemeral_storage),
                Ref(AWS_NO_VALUE),
            )
            if 0 < self.task_ephemeral_storage >= 21
            else Ref(AWS_NO_VALUE),
            InferenceAccelerators=Ref(AWS_NO_VALUE),
            IpcMode=Ref(AWS_NO_VALUE),
            Family=Ref(ecs_params.SERVICE_NAME),
            TaskRoleArn=self.task_role.arn,
            ExecutionRoleArn=self.exec_role.arn,
            ContainerDefinitions=[s.container_definition for s in self.services],
            RequiresCompatibilities=ecs_conditions.use_external_lt_con(
                ["EXTERNAL"], ["EC2", "FARGATE"]
            ),
            Tags=Tags(
                {
                    "Name": Ref(ecs_params.SERVICE_NAME),
                    "Environment": Ref(AWS_STACK_NAME),
                    "compose-x::family": self.name,
                    "compose-x::logical_name": self.logical_name,
                }
            ),
        )
        for service in self.services:
            service.container_definition.DockerLabels.update(
                {
                    "container_name": service.container_name,
                    "ecs_task_family": Ref(ecs_params.SERVICE_NAME),
                }
            )

    def add_containers_images_cfn_parameters(self):
        """
        Adds parameters to the stack and set values for each service/container in the family definition
        """
        if not self.template:
            return
        for service in self.services:
            self.stack_parameters.update({service.image_param.title: service.image})
            if service.image_param.title not in self.template.parameters:
                self.template.add_parameter(service.image_param)

    def refresh_container_logging_definition(self):
        for service in self.services:
            c_def = service.container_definition
            logging_def = c_def.LogConfiguration
            logging_def.Options.update(self.task_logging_options)

    def init_task_definition(self):
        """
        Initialize the ECS TaskDefinition

        * Sets Compute settings
        * Sets the TaskDefinition using current services/ContainerDefinitions
        * Update the logging configuration for the containers.
        """
        self.set_task_compute_parameter()
        self.set_task_definition()
        self.refresh_container_logging_definition()

    def set_family_hostname(self):
        svcs_hostnames = any(svc.family_hostname for svc in self.services)
        if not svcs_hostnames or not self.family_hostname:
            LOG.debug(
                f"{self.name} - No ecs.task.family.hostname defined on any of the services. "
                f"Setting to {self.family_hostname}"
            )
            return
        potential_svcs = []
        for svc in self.services:
            if (
                svc.family_hostname
                and hasattr(svc, "container_definition")
                and svc.container_definition.Essential
            ):
                potential_svcs.append(svc)
        uniq = []
        for svc in potential_svcs:
            if svc.family_hostname not in uniq:
                uniq.append(svc.family_hostname)
        self.family_hostname = uniq[0].lower().replace("_", "-")
        if len(uniq) > 1:
            LOG.warning(
                f"{self.name} more than one essential container has ecs.task.family.hostname set. "
                f"Using the first one {uniq[0]}"
            )

    def update_family_subnets(self, settings):
        """
        Method to update the stack parameters

        :param ecs_composex.common.settings.ComposeXSettings settings:
        """
        network_names = list(self.ecs_service.network.networks.keys())
        for network in settings.networks:
            if network.name in network_names:
                self.stack_parameters.update(
                    {
                        APP_SUBNETS.title: Join(
                            ",",
                            FindInMap("Network", network.subnet_name, "Ids"),
                        )
                    }
                )
                LOG.info(
                    f"{self.name} - {APP_SUBNETS.title} set to {network.subnet_name}"
                )

    def upload_services_env_files(self, settings):
        """
        Method to go over each service and if settings are to upload files to S3, will create objects and update the
        container definition for env_files accordingly.

        :param ecs_composex.common.settings.ComposeXSettings settings:
        :return:
        """
        if settings.no_upload:
            return
        elif settings.for_cfn_macro:
            LOG.warning(
                f"{self.name} When running as a Macro, you cannot upload environment files."
            )
            return
        for service in self.services:
            env_files = []
            for env_file in service.env_files:
                with open(env_file, "r") as file_fd:
                    file_body = file_fd.read()
                object_name = path.basename(env_file)
                try:
                    upload_file(
                        body=file_body,
                        bucket_name=settings.bucket_name,
                        mime="text/plain",
                        prefix=f"{FILE_PREFIX}/env_files",
                        file_name=object_name,
                        settings=settings,
                    )
                    LOG.info(
                        f"{self.name}.env_files - Successfully uploaded {env_file} to S3"
                    )
                except Exception:
                    LOG.error(f"Failed to upload env file {object_name}")
                    raise
                file_path = Sub(
                    f"arn:${{{AWS_PARTITION}}}:s3:::{settings.bucket_name}/{FILE_PREFIX}/env_files/{object_name}"
                )
                env_files.append(EnvironmentFile(Type="s3", Value=file_path))
            if not hasattr(service.container_definition, "EnvironmentFiles"):
                setattr(service.container_definition, "EnvironmentFiles", env_files)
            else:
                service.container_definition.EnvironmentFiles += env_files
            if (
                env_files
                and self.template
                and "S3EnvFilesAccess" not in self.template.resources
            ):
                self.template.add_resource(
                    PolicyType(
                        "S3EnvFilesAccess",
                        PolicyName="S3EnvFilesAccess",
                        PolicyDocument={
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Action": "s3:GetObject",
                                    "Effect": "Allow",
                                    "Resource": Sub(
                                        f"arn:${{{AWS_PARTITION}}}:s3:::{settings.bucket_name}/*"
                                    ),
                                }
                            ],
                        },
                        Roles=[
                            self.exec_role.name,
                            self.task_role.name,
                        ],
                    )
                )

    def set_repository_credentials(self, settings):
        """
        Method to go over each service and identify which ones have credentials to pull the Docker image from a private
        repository

        :param ecs_composex.common.settings.ComposeXSettings settings:
        :return:
        """
        for service in self.services:
            if not service.x_repo_credentials:
                continue
            if service.x_repo_credentials.startswith("arn:aws"):
                secret_arn = service.x_repo_credentials
            elif service.x_repo_credentials.startswith("secrets::"):
                secret_name = service.x_repo_credentials.split("::")[-1]
                secret_arn = identify_repo_credentials_secret(
                    settings, self, secret_name
                )
            else:
                raise ValueError(
                    "The secret for private repository must be either an ARN or the name of a secret defined in secrets"
                )
            setattr(
                service.container_definition,
                "RepositoryCredentials",
                RepositoryCredentials(CredentialsParameter=secret_arn),
            )
            policy = PolicyType(
                "AccessToRepoCredentialsSecret",
                PolicyName="AccessToRepoCredentialsSecret",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["secretsmanager:GetSecretValue"],
                            "Sid": "AccessToRepoCredentialsSecret",
                            "Resource": [secret_arn],
                        }
                    ],
                },
                Roles=[self.exec_role.name],
            )
            if self.template and policy.title not in self.template.resources:
                self.template.add_resource(policy)

    def set_services_mount_points(self):
        """
        Method to set the mount points to the Container Definition of the defined service
        """
        for service in self.services:
            mount_points = []
            if not hasattr(service.container_definition, "MountPoints"):
                setattr(service.container_definition, "MountPoints", mount_points)
            else:
                mount_points = getattr(service.container_definition, "MountPoints")
            for volume in service.volumes:
                mnt_point = MountPoint(
                    ContainerPath=volume["target"],
                    ReadOnly=volume["read_only"],
                    SourceVolume=volume["volume"].volume_name,
                )
                mount_points.append(mnt_point)

    def define_shared_volumes(self):
        """
        Method to create a list of shared volumes within the task family and set the volume to shared = True if not.

        :return: list of shared volumes within the task definition
        :rtype: list
        """
        family_task_volumes = []
        for service in self.services:
            for volume in service.volumes:
                if volume["volume"] and volume["volume"] not in family_task_volumes:
                    family_task_volumes.append(volume["volume"])
                else:
                    volume["volume"].is_shared = True
        return family_task_volumes

    def set_volumes(self):
        """
        Method to create the volumes definition to the Task Definition

        :return:
        """
        family_task_volumes = self.define_shared_volumes()
        family_definition_volumes = []
        if not hasattr(self.task_definition, "Volumes"):
            setattr(self.task_definition, "Volumes", family_definition_volumes)
        else:
            family_definition_volumes = getattr(self.task_definition, "Volumes")
        for volume in family_task_volumes:
            if volume.type == "volume" and volume.driver == "local":
                volume.cfn_volume = Volume(
                    Host=Ref(AWS_NO_VALUE),
                    Name=volume.volume_name,
                    DockerVolumeConfiguration=If(
                        ecs_conditions.USE_FARGATE_CON_T,
                        Ref(AWS_NO_VALUE),
                        DockerVolumeConfiguration(
                            Scope="task" if not volume.is_shared else "shared",
                            Autoprovision=Ref(AWS_NO_VALUE)
                            if not volume.is_shared
                            else True,
                        ),
                    ),
                )
            if volume.cfn_volume:
                family_definition_volumes.append(volume.cfn_volume)
        self.set_services_mount_points()

    def set_service_update_config(self):
        """
        Method to determine the update_config for the service. When a family has multiple containers, this applies
        to all tasks.
        """
        min_percents = [
            int(service.definition["x-aws-min_percent"])
            for service in self.services
            if keypresent("x-aws-min_percent", service.definition)
        ]
        max_percents = [
            int(service.definition["x-aws-max_percent"])
            for service in self.services
            if keypresent("x-aws-max_percent", service.definition)
        ]
        if min_percents:
            minis_sum = sum(min_percents)
            if not minis_sum:
                family_min_percent = 0
            else:
                family_min_percent = minis_sum / len(min_percents)
        else:
            family_min_percent = 100

        if max_percents:
            maxis_sum = sum(max_percents)
            if not maxis_sum:
                family_max_percent = 0
            else:
                family_max_percent = maxis_sum / len(max_percents)
        else:
            family_max_percent = 200
        rollback = True
        actions = [
            service.update_config["failure_action"] != "rollback"
            for service in self.services
            if service.update_config
            and keyisset("failure_action", service.update_config)
        ]
        if any(actions):
            rollback = False
        self.deployment_config.update(
            {
                "MinimumHealthyPercent": family_min_percent,
                "MaximumPercent": family_max_percent,
                "RollBack": rollback,
            }
        )

    def set_prometheus_containers_insights(
        self, service: ComposeService, prometheus_config: dict, insights_options: dict
    ):
        """
        Sets prometheus configuration to export to ECS Containers Insights
        """
        if keyisset("ContainersInsights", prometheus_config):
            config = service.definition["x-prometheus"]["ContainersInsights"]
            for key in insights_options.keys():
                if keyisset(key, config):
                    insights_options[key] = config[key]
            if keyisset("CustomRules", config):
                insights_options["CustomRules"] = config["CustomRules"]
                LOG.info(
                    f"{self.name} - Prometheus CustomRules options set for {service.name}"
                )

    def set_prometheus(self):
        """
        Reviews services config
        :return:
        """
        from ecs_composex.ecs.ecs_prometheus import add_cw_agent_to_family

        insights_options = {
            "CollectForAppMesh": False,
            "CollectForJavaJmx": False,
            "CollectForNginx": False,
            "EnableTasksDiscovery": False,
            "EnableCWAgentDebug": False,
            "AutoAddNginxPrometheusExporter": False,
            "ScrapingConfiguration": False,
        }
        for service in self.services:
            if keyisset("x-prometheus", service.definition):
                prometheus_config = service.definition["x-prometheus"]
                self.set_prometheus_containers_insights(
                    service, prometheus_config, insights_options
                )
        if any(insights_options.values()):
            add_cw_agent_to_family(self, **insights_options)

    def merge_capacity_providers(self):
        """
        Merge capacity providers set on the services of the task family if service is not sidecar
        """
        task_config = {}
        for svc in self.services:
            if not svc.capacity_provider_strategy or svc.is_aws_sidecar:
                continue
            for provider in svc.capacity_provider_strategy:
                if provider["CapacityProvider"] not in task_config.keys():
                    name = provider["CapacityProvider"]
                    task_config[name] = {
                        "Base": [],
                        "Weight": [],
                        "CapacityProvider": name,
                    }
                    task_config[name]["Base"].append(
                        set_else_none("Base", provider, alt_value=0)
                    )
                    task_config[name]["Weight"].append(
                        set_else_none("Weight", provider, alt_value=0)
                    )
        for count, provider in enumerate(task_config.values()):
            if count == 0:
                provider["Base"] = int(max(provider["Base"]))
            elif count > 0 and keypresent("Base", provider):
                del provider["Base"]
                LOG.warning(
                    f"{self.name}.x-ecs Only one capacity provider can have a base value. "
                    f"Deleting for {provider['CapacityProvider']}"
                )
            provider["Weight"] = int(max(provider["Weight"]))
        self.ecs_capacity_providers = list(task_config.values())

    def set_launch_type_from_cluster_and_service(self):
        if all(
            provider["CapacityProvider"] in ["FARGATE", "FARGATE_SPOT"]
            for provider in self.ecs_capacity_providers
        ):
            LOG.debug(
                f"{self.name} - Cluster and Service use Fargate only. Setting to FARGATE_PROVIDERS"
            )
            self.launch_type = "FARGATE_PROVIDERS"
        else:
            self.launch_type = "SERVICE_MODE"
            LOG.debug(
                f"{self.name} - Using AutoScaling Based Providers",
                [
                    provider["CapacityProvider"]
                    for provider in self.ecs_capacity_providers
                ],
            )

    def set_launch_type_from_cluster_only(self, cluster):
        if any(
            provider in ["FARGATE", "FARGATE_SPOT"]
            for provider in cluster.default_strategy_providers
        ):
            self.launch_type = "FARGATE_PROVIDERS"
            LOG.debug(
                f"{self.name} - Defaulting to FARGATE_PROVIDERS as "
                "FARGATE[_SPOT] is found in the cluster default strategy"
            )
        else:
            self.launch_type = "CLUSTER_MODE"
            LOG.debug(
                f"{self.name} - Cluster uses non Fargate Capacity Providers. Setting to Cluster default"
            )
            self.launch_type = "CLUSTER_MODE"

    def set_service_launch_type(self, cluster):
        """
        Sets the LaunchType value for the ECS Service
        """
        if self.launch_type == "EXTERNAL":
            return
        if self.ecs_capacity_providers and cluster.capacity_providers:
            self.set_launch_type_from_cluster_and_service()
        elif not self.ecs_capacity_providers and cluster.capacity_providers:
            self.set_launch_type_from_cluster_only(cluster)
        self.set_family_ecs_service_lt()

    def set_family_ecs_service_lt(self):
        """
        Sets Launch Type for family
        """
        if not self.service_definition:
            LOG.warning(f"{self.name} - ECS Service not yet defined. Skipping")
            return
        if (
            self.launch_type == "FARGATE_PROVIDERS"
            or self.launch_type == "SERVICE_MODE"
        ):
            cfn_capacity_providers = [
                CapacityProviderStrategyItem(**props)
                for props in self.ecs_capacity_providers
            ]
            if isinstance(self.service_definition, EcsService):
                setattr(
                    self.service_definition,
                    "CapacityProviderStrategy",
                    cfn_capacity_providers,
                )
        elif (
            self.launch_type == "FARGATE"
            or self.launch_type == "CLUSTER_MODE"
            or self.launch_type == "EC2"
            or self.launch_type == "EXTERNAL"
        ):
            setattr(
                self.service_definition,
                "CapacityProviderStrategy",
                Ref(AWS_NO_VALUE),
            )

    def validate_capacity_providers(self, cluster):
        """
        Validates that the defined ecs_capacity_providers are all available in the ECS Cluster Providers

        :param cluster: The cluster object
        :raises: ValueError if not all task family providers in the cluster providers
        :raises: TypeError if cluster_providers not a list
        """
        if not self.ecs_capacity_providers and not cluster.capacity_providers:
            LOG.debug(
                f"{self.name} - No capacity providers specified in task definition nor cluster"
            )
            return True
        elif not cluster.capacity_providers:
            LOG.debug(f"{self.name} - No capacity provider set for cluster")
            return True
        cap_names = [cap["CapacityProvider"] for cap in self.ecs_capacity_providers]
        if not all(cap_name in ["FARGATE", "FARGATE_SPOT"] for cap_name in cap_names):
            raise ValueError(
                f"{self.name} - You cannot mix FARGATE capacity provider with AutoScaling Capacity Providers",
                cap_names,
            )
        if not isinstance(cluster.capacity_providers, list):
            raise TypeError("clusters_providers must be a list")

        elif not all(provider in cluster.capacity_providers for provider in cap_names):
            raise ValueError(
                "Providers",
                cap_names,
                "not defined in ECS Cluster providers. Valid values are",
                cluster.capacity_providers,
            )

    def validate_compute_configuration_for_task(self, settings):
        """
        Function to perform a final validation of compute before rendering.
        :param ecs_composex.common.settings.ComposeXSettings settings:
        """
        if self.launch_type and self.launch_type == "EXTERNAL":
            LOG.debug(f"{self.name} - Already set to EXTERNAL")
            return
        if settings.ecs_cluster.platform_override:
            self.launch_type = settings.ecs_cluster.platform_override
            if hasattr(
                self.service_definition, "CapacityProviderStrategy"
            ) and isinstance(self.service_definition.CapacityProviderStrategy, list):
                LOG.warning(
                    f"{self.name} - Due to Launch Type override to {settings.ecs_cluster.platform_override}"
                    ", ignoring CapacityProviders"
                    f"{[cap.CapacityProvider for cap in self.service_definition.CapacityProviderStrategy]}"
                )
                setattr(
                    self.service_definition,
                    "CapacityProviderStrategy",
                    Ref(AWS_NO_VALUE),
                )
        else:
            self.merge_capacity_providers()
            self.validate_capacity_providers(settings.ecs_cluster)
            self.set_service_launch_type(settings.ecs_cluster)
            LOG.debug(
                f"{self.name} - Updated {ecs_params.LAUNCH_TYPE.title} to"
                f" {self.launch_type}"
            )
        if self.stack:
            self.stack.Parameters.update(
                {ecs_params.LAUNCH_TYPE.title: self.launch_type}
            )

    def set_service_dependency_on_all_iam_policies(self):
        """
        Function to ensure the Service does not get created/updated before all IAM policies were set completely
        """
        if not self.ecs_service.ecs_service:
            return
        policies = [
            p.title
            for p in self.template.resources.values()
            if isinstance(p, PolicyType)
        ]
        if hasattr(self.ecs_service.ecs_service, "DependsOn"):
            depends_on = getattr(self.ecs_service.ecs_service, "DependsOn")
            for policy in policies:
                if policy not in depends_on:
                    depends_on.append(policy)
        else:
            setattr(self.ecs_service.ecs_service, "DependsOn", policies)
        LOG.debug(self.ecs_service.ecs_service.DependsOn)
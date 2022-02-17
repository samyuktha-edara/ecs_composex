#  -*- coding: utf-8 -*-
# SPDX-License-Identifier: MPL-2.0
# Copyright 2020-2022 John Mille <john@compose-x.io>

"""
Module to manage IAM policies to grant access to ECS Services to DynamodbTables
"""

from ecs_composex.common import LOG
from ecs_composex.dynamodb.dynamodb_params import TABLE_ARN
from ecs_composex.resource_settings import (
    handle_lookup_resource,
    handle_resource_to_services,
)


def dynamodb_to_ecs(resources, services_stack, res_root_stack, settings):
    """
    Function to apply SQS settings to ECS Services
    :return:
    """
    for resource_name, resource in resources.items():
        LOG.info(f"{resource.module_name}.{resource_name} - Linking to services")
        if not resource.mappings and resource.cfn_resource:
            handle_resource_to_services(
                resource,
                services_stack,
                res_root_stack,
                settings,
                TABLE_ARN,
                list(resource.attributes_outputs.keys()),
                nested=False,
            )
        elif not resource.cfn_resource and resource.mappings:
            handle_lookup_resource(
                settings,
                resource,
                TABLE_ARN,
            )

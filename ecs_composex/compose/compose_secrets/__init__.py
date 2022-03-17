#   -*- coding: utf-8 -*-
#  SPDX-License-Identifier: MPL-2.0
#  Copyright 2020-2022 John Mille <john@compose-x

"""
Package to manage docker-compose secrets
"""

from copy import deepcopy

from compose_x_common.aws.kms import KMS_KEY_ARN_RE
from compose_x_common.aws.secrets_manager import get_secret_name_from_arn
from compose_x_common.compose_x_common import keyisset
from troposphere import AWS_ACCOUNT_ID, AWS_PARTITION, AWS_REGION, FindInMap, Sub
from troposphere.ecs import Secret as EcsSecret

from ecs_composex.common import LOG, NONALPHANUM
from ecs_composex.ecs.ecs_params import EXEC_ROLE_T
from ecs_composex.secrets.secrets_aws import lookup_secret_config
from ecs_composex.secrets.secrets_params import RES_KEY, XRES_KEY

from .helpers import define_env_var_name


class ComposeSecret(object):
    """
    Class to represent a Compose secret.
    """

    x_key = XRES_KEY
    main_key = "secrets"
    map_kms_name = "KmsKeyId"
    map_arn_name = "Arn"
    map_name_name = "Name"
    json_keys_key = "JsonKeys"
    links_key = "LinksTo"
    map_name = RES_KEY
    allowed_keys = ["Name", "Lookup"]
    valid_keys = [json_keys_key, links_key] + allowed_keys

    def __init__(self, name, definition, settings):
        """
        Method to init Secret for ECS ComposeX

        :param str name:
        :param dict definition:
        :param ecs_composex.common.settings.ComposeXSettings settings:
        """
        self.services = []
        if not any(key in definition[self.x_key].keys() for key in self.allowed_keys):
            raise KeyError(
                "You must define at least one of",
                self.allowed_keys,
                "Got",
                definition.keys(),
            )
        elif not all(key in self.valid_keys for key in definition[self.x_key].keys()):
            raise KeyError(
                "Only valid keys are",
                self.valid_keys,
                "Got",
                definition[self.x_key].keys(),
            )
        self.name = name
        self.logical_name = NONALPHANUM.sub("", self.name)
        self.definition = deepcopy(definition)
        self.links = [EXEC_ROLE_T]
        self.arn = None
        self.iam_arn = None
        self.aws_name = None
        self.kms_key = None
        self.kms_key_arn = None
        self.ecs_secret = []
        self.mapping = {}
        if not keyisset("Lookup", self.definition[self.x_key]):
            self.define_names_from_import()
        else:
            self.define_names_from_lookup(settings.session)

        self.define_links()
        if self.mapping:
            settings.secrets_mappings.update({self.logical_name: self.mapping})
            self.add_json_keys()

    def add_json_keys(self):
        """
        Add secrets definitions based on JSON secret keys
        """
        if not keyisset(self.json_keys_key, self.definition[self.x_key]):
            return
        unfiltered_secrets = self.definition[self.x_key][self.json_keys_key]
        filtered_secrets = [
            dict(y) for y in set(tuple(x.items()) for x in unfiltered_secrets)
        ]
        old_secrets = deepcopy(self.ecs_secret)
        self.ecs_secret = []
        for secret_key in filtered_secrets:
            json_key = secret_key["SecretKey"]
            secret_name = define_env_var_name(secret_key)
            if isinstance(self.arn, str):
                self.ecs_secret.append(
                    EcsSecret(Name=secret_name, ValueFrom=f"{self.arn}:{json_key}::")
                )
            elif isinstance(self.arn, Sub):
                self.ecs_secret.append(
                    EcsSecret(
                        Name=secret_name,
                        ValueFrom=Sub(
                            f"arn:${{{AWS_PARTITION}}}:secretsmanager:${{{AWS_REGION}}}:${{{AWS_ACCOUNT_ID}}}:"
                            f"secret:${{SecretName}}:{json_key}::",
                            SecretName=FindInMap(
                                self.map_name,
                                self.logical_name,
                                self.map_name_name,
                            ),
                        ),
                    )
                )
            elif isinstance(self.arn, FindInMap):
                self.ecs_secret.append(
                    EcsSecret(
                        Name=secret_name,
                        ValueFrom=Sub(
                            f"${{SecretArn}}:{json_key}::",
                            SecretArn=FindInMap(
                                self.map_name,
                                self.logical_name,
                                self.map_arn_name,
                            ),
                        ),
                    )
                )
        if not self.ecs_secret:
            self.ecs_secret = old_secrets

    def define_names_from_import(self):
        """
        Define the names from docker-compose file content
        """
        if not keyisset(self.map_name_name, self.definition[self.x_key]):
            raise KeyError(
                f"Missing {self.map_name_name} when doing non-lookup import for {self.name}"
            )
        name_input = self.definition[self.x_key][self.map_name_name]
        if name_input.startswith("arn:"):
            self.aws_name = get_secret_name_from_arn(
                self.definition[self.x_key][self.map_name_name]
            )
            self.mapping = {
                self.map_arn_name: name_input,
                self.map_name_name: self.aws_name,
            }
        else:
            self.aws_name = name_input
            self.mapping = {self.map_name_name: self.aws_name}
            self.arn = Sub(
                f"arn:${{{AWS_PARTITION}}}:secretsmanager:${{{AWS_REGION}}}:${{{AWS_ACCOUNT_ID}}}:"
                "secret:${SecretName}",
                SecretName=FindInMap(
                    self.map_name, self.logical_name, self.map_name_name
                ),
            )
            self.iam_arn = Sub(
                f"arn:${{{AWS_PARTITION}}}:secretsmanager:${{{AWS_REGION}}}:${{{AWS_ACCOUNT_ID}}}:"
                "secret:${SecretName}*",
                SecretName=FindInMap(
                    self.map_name, self.logical_name, self.map_name_name
                ),
            )
            self.ecs_secret = [EcsSecret(Name=self.name, ValueFrom=self.arn)]
        if keyisset(self.map_kms_name, self.definition):
            if not self.definition[self.map_kms_name].startswith(
                "arn:"
            ) or not KMS_KEY_ARN_RE.match(self.definition[self.map_kms_name]):
                LOG.error(
                    f"secrets.{self.name} - When specifying {self.map_kms_name} you must specify the full ARN"
                )
            else:
                self.mapping[self.map_kms_name] = self.definition[self.map_kms_name]
                self.kms_key_arn = FindInMap(
                    self.map_name, self.logical_name, self.map_kms_name
                )

    def define_names_from_lookup(self, session):
        """
        Method to Lookup the secret based on its tags.
        """
        lookup_info = self.definition[self.x_key]["Lookup"]
        if keyisset("Name", self.definition[self.x_key]):
            lookup_info["Name"] = self.definition[self.x_key]["Name"]
        secret_config = lookup_secret_config(self.logical_name, lookup_info, session)
        self.aws_name = get_secret_name_from_arn(secret_config[self.logical_name])
        self.arn = secret_config[self.logical_name]
        self.iam_arn = secret_config[self.logical_name]
        if keyisset("KmsKeyId", secret_config) and not secret_config[
            "KmsKeyId"
        ].startswith("alias"):
            self.kms_key = secret_config["KmsKeyId"]
        elif keyisset("KmsKeyId", secret_config) and secret_config[
            "KmsKeyId"
        ].startswith("alias"):
            LOG.warning(
                f"secrets.{self.name} - The KMS Key retrieved is a KMS Key Alias, not importing."
            )

        self.mapping = {
            self.map_arn_name: secret_config[self.logical_name],
            self.map_name_name: secret_config[self.map_name_name],
        }
        if self.kms_key:
            self.mapping[self.map_kms_name] = self.kms_key
            self.kms_key_arn = FindInMap(
                self.map_name, self.logical_name, self.map_kms_name
            )
        self.arn = FindInMap(self.map_name, self.logical_name, self.map_arn_name)
        self.ecs_secret = [EcsSecret(Name=self.name, ValueFrom=self.arn)]

    def define_links(self):
        """
        Defines which IAM role to assign the secrets access policy to. Defaults to exec role
        """
        if keyisset(self.links_key, self.definition[self.x_key]):
            self.links = self.definition[self.x_key][self.links_key]
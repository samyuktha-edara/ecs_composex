
.. meta::
    :description: ECS Compose-X AWS Load Balancing syntax reference
    :keywords: AWS, AWS ECS, Docker, Compose, docker-compose, AWS ELB, ELBv2, ALB, NLB

.. _elbv2_syntax_reference:

=========
x-elbv2
=========

.. code-block:: yaml

    x-elbv2:
      lbA:
        Properties: {}
        MacroParameters: {}
        Listeners: []
        Services: []
        DnsAliases: []
        Settings: {}


Create new ELBv2 (ALB/NLB) and configure traffic to send to the services.
You can define conditions to distinguish services from each other and allow to re-use the same ELBv2 (mostly applies to ALBs)
to use it as a smart reverse proxy.

.. hint::

    Supports OIDC and Cognito Conditions. Refer to :ref:`cognito_userpool_syntax_reference` for more details.


Properties
==========

For this particular resource, the only attributes that match the CFN definition that ECS Compose-X will import are

* `Scheme`_
* `Type`_
* `LoadBalancerAttributes`_
* `Tags`_

All other settings are automatically generated for you based on the network and security definitions you have defined in
the services and targets section.

Subnets associations can be overridden in the Settings.Subnets section. See :ref:`common_settings_subnets` for more details.

.. hint::

    For Application Load Balancers, a new security group will be created automatically.
    Subnets are selected automatically based on the scheme you indicated.
    If selected a public NLB, the EIP addressed will automatically be provisioned too.


DnsAliases
===========

To create DNS records in Route53 pointing to your ELBv2, see :ref:`x_route53-x_elbv2`

MacroParameters
================

.. code-block:: yaml
    :caption: ELBv2 Macro Parameters

    timeout_seconds: int
    desync_mitigation_mode: str
    drop_invalid_header_fields: bool
    http2: bool
    cross_zone: bool
    Ingress: {}


Attributes shortcuts
--------------------------
These settings are just a shorter notation for the `LB Attributes`_

+----------------------------+-------------------------------------------------+---------+
| Shorthand                  | AttributeName                                   | LB Type |
+============================+=================================================+=========+
| timeout_seconds            | idle_timeout.timeout_seconds                    | ALB     |
+----------------------------+-------------------------------------------------+---------+
| desync_mitigation_mode     | routing.http.desync_mitigation_mode             | ALB     |
+----------------------------+-------------------------------------------------+---------+
| drop_invalid_header_fields | routing.http.drop_invalid_header_fields.enabled | ALB     |
+----------------------------+-------------------------------------------------+---------+
| http2                      | routing.http2.enabled                           | ALB     |
+----------------------------+-------------------------------------------------+---------+
| cross_zone                 | load_balancing.cross_zone.enabled               | NLB     |
+----------------------------+-------------------------------------------------+---------+



.. _load_balancers_ingress_syntax_ref:

Ingress
-------

Similar syntax as for ECS Services Ingress, allow you to define Ingress. See the `Ingress JSON Schema definition`_.

.. tip::

    When using NLB, ingress must be defined at the service level, as NLB do not have a SecurityGroup

.. code-block:: yaml
    :caption: Ingress Syntax

    Ingress:
      ExtSources: []
      AwsSources: []

.. code-block:: yaml
    :caption: ExtSources syntax

    ExtSources:
      - Name: str (if any non alphanumeric character set, will be deleted)
        Description: str
        IPv4: str

.. code-block:: yaml
    :caption: AwsSources syntax

    AwsSources:
      - Type: SecurityGroup|PrefixList (str)
        Id: sg-[a-z0-9]+|pl-[a-z0-9]+
        Lookup: {}

.. tip::

    You can use either Id or Lookup to identify the SecurityGroups.
    Check out the :ref:`lookup_syntax_reference` syntax reference


Services
========

This follows the regular pattern of having the name of the service and access, only this time in a slightly different format.
The services represent the `Target Group`_ definition of your service. Once again, in an attempt to keep things simple,
you do not have to indicate all of the settings exactly as CFN does.

The Targets will automatically be pointing towards the ECS Service tasks.

Syntax
------

.. code-block:: yaml

    name: <family name:container name>
    protocol: <str>
    port : <int>
    healthcheck: <str>
    TargetGroupAttributes: list|map

`JSON Schema definition <https://github.com/compose-x/ecs_composex_specs/blob/main/ecs_composex_specs/x-elbv2.spec.json#L38>`__

name
^^^^^^^^^^^^^^^^^^

Given that you can now re-use one of the service in the docker-compose file multiple times for multiple ECS Services
in multiple Task definitions, and ECS to ELBv2 supports to route traffic to a specific container in the task definition,
you have to indicate the service name in the following format

.. code-block::

    # name: <family_name>:<service_name>
    name: youtoo:app01
    name: app03:app03

.. hint::

    If you service is not associated to a family via deploy labels, the family name is the same as the service name.


protocol
^^^^^^^^^^^^^^^^^^

The Target Group protocol


port
^^^^^^^^^^^^^^^^^^

The port of the target to send the traffic to

.. hint::

    This port is the port used by the Target Group to send traffic to, which can be different to your healthcheck port.


healthcheck
^^^^^^^^^^^^^^^^^^


.. code-block:: yaml
    :caption: shorthand regular expression

    x-elbv2:
      lb:
        Services:
          - healthcheck: (port:protocol)(:healthy_count:unhealthy_count:intervals:timeout)?(:path:http_codes)?


.. code-block:: yaml
    :caption: full definition with the properties

    x-elbv2:
      Services:
        - name: family:service
          healthcheck:
            HealthCheckEnabled:
            HealthCheckIntervalSeconds:
            HealthCheckPath:
            HealthCheckPort:
            HealthCheckProtocol:
            HealthCheckTimeoutSeconds:
            HealthyThresholdCount:
            UnhealthyThresholdCount:
            Matcher : <Matcher>


The healthcheck can be defined either as a string, will set the properties values accordingly,
or as a mapping, using the same healthcheck as the ones defined in  `Target Group`_ definition.


.. tip::

    ECS Compose-X will log a warning when it detects an invalid value, and corrects it to the valid one.

TargetGroupAttributes
======================

In order to set Target Group specific settings, you can use `CFN TargetGroupAttributes`_ properties.

In AWS CFN, it is a list of Key/Value objects, so compose-x supports it that way.

.. code-block:: yaml

    Services:
      - name: app03:app03
        port: 5000
        healthcheck: 5000:TCP:7:2:15:5
        protocol: TCP
        TargetGroupAttributes:
          - Key: deregistration_delay.timeout_seconds
            Value: "30"
          - Key: proxy_protocol_v2.enabled
            Value: "true"
          - Key: preserve_client_ip.enabled
            Value: "true"

But in order to **avoid duplicates** and make the merge of compose files easier, you can also defined these properties
into a map/dict structure and compose-x will automatically convert it to the CFN Expected format.

.. code-block:: yaml

    Services:
      - name: app03:app03
        port: 5000
        healthcheck: 5000:TCP:7:2:15:5
        protocol: TCP
        TargetGroupAttributes:
          deregistration_delay.timeout_seconds: "30"
          proxy_protocol_v2.enabled: "true"
          preserve_client_ip.enabled: "true"


.. hint::

    Compose-X will, based on the type of Load Balancer, ensure that the properties you set are compatible with the
    LoadBalancer type and the values are valid / in range.

    +-----------------------------------------------------+---------+------------------------------+
    | Property Name                                       | LB Type | Allowed Values               |
    +-----------------------------------------------------+---------+------------------------------+
    | deregistration_delay.timeout_seconds                | ALL     | Range(0,3600)                |
    |                                                     |         | Seconds                      |
    +-----------------------------------------------------+---------+------------------------------+
    | stickiness.enabled                                  | * ALB   | * "true"                     |
    |                                                     | * NLB   | * "false"                    |
    +-----------------------------------------------------+---------+------------------------------+
    | stickiness.type                                     | * ALB   | ALB:                         |
    |                                                     | * NLB   |  * lb_cookie                 |
    |                                                     |         |  * app_cookie                |
    |                                                     |         | NLB:                         |
    |                                                     |         |  * source_ip                 |
    +-----------------------------------------------------+---------+------------------------------+
    | load_balancing.algorithm.type                       | * ALB   | * round_robin                |
    |                                                     |         | * least_outstanding_requests |
    +-----------------------------------------------------+---------+------------------------------+
    | slow_start.duration_seconds                         | * ALB   | Range(30-900)                |
    |                                                     |         | Seconds                      |
    +-----------------------------------------------------+---------+------------------------------+
    | stickiness.app_cookie.cookie_name                   | * ALB   | String                       |
    |                                                     |         | Cannot use or start with     |
    |                                                     |         | * AWSALB                     |
    |                                                     |         | * AWSALBAPP                  |
    |                                                     |         | * AWSALBTG                   |
    +-----------------------------------------------------+---------+------------------------------+
    | stickiness.app_cookie.duration_seconds              | * ALB   | Range(1,604800)              |
    |                                                     |         | Seconds                      |
    +-----------------------------------------------------+---------+------------------------------+
    | stickiness.lb_cookie.duration_seconds               | * ALB   | Range(1,604800)              |
    |                                                     |         | Seconds                      |
    +-----------------------------------------------------+---------+------------------------------+
    | lambda.multi_value_headers.enabled                  | * ALB   | * "true"                     |
    | * Works only for Lambda targets                     |         | * "false"                    |
    +-----------------------------------------------------+---------+------------------------------+
    | deregistration_delay.connection_termination.enabled | * NLB   | * "true"                     |
    |                                                     |         | * "false"                    |
    +-----------------------------------------------------+---------+------------------------------+
    | preserve_client_ip.enabled                          | * NLB   | * "true"                     |
    |                                                     |         | * "false"                    |
    +-----------------------------------------------------+---------+------------------------------+
    | proxy_protocol_v2.enabled                           | * NLB   | * "true"                     |
    |                                                     |         | * "false"                    |
    +-----------------------------------------------------+---------+------------------------------+

    .. seealso::

        `Target Group Attributes`_


Listeners
=========

You can define in a very simple way your `Listener definition`_ and cross-reference other resources, here, the services
and ACM certificates you might be creating.

It has its own set of properties, custom to ECS ComposeX.

The following properties are identical to the original CFN definition.

* `Port <https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html#cfn-elasticloadbalancingv2-listener-port>`_
* `Protocol <https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html#cfn-elasticloadbalancingv2-listener-protocol>`_
* `SslPolicy <https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html#cfn-elasticloadbalancingv2-listener-sslpolicy>`_
* `Certificates <https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html#cfn-elasticloadbalancingv2-listener-certificates>`_

`JSON Schema definition <https://github.com/compose-x/ecs_composex_specs/blob/main/ecs_composex_specs/x-elbv2.spec.json#L82>`__

.. hint::

    For certificates, you can also use **x-acm** to refer to an ACM certificate you are creating with this stack.
    It will automatically import the Certificate ARN and map it once created.

.. hint::

    You can re-use the same ACM certificate defined in x-acm for multiple listeners. Make sure to have all the Alt. Subjects you need!

.. warning::

    The certificate ARN must be valid when set, however, we are not checking that it actually exists.(yet)


Listener Targets
=================

List of targets to send the requests to. These are equivalent to ELBv2::TargetGroup

.. code-block:: yaml

    name: <service_name> ie. app03:app03
    access: <domain name and or path> ie. domain.net/path
    cognito_auth: AuthenticateCognitoConfig


This represents the targets and simultaneously the Listener Rules to apply so that you can point to multiple services
at once and implement these rules.

name
-----

The name of the family and service in that family to send the requests to.


access
------

Allows you to define the conditions based on the path or domain name (or combination of both) that should be in place
to forward requests.

If you only define the domain name, any path in that domain will be what's matched.

AuthenticateCognitoConfig
---------------------------

Defines the `AuthenticateCognitoConfig`_ requirement condition / action.


AuthenticateOidcConfig
-----------------------

Similar to `AuthenticateCognitoConfig`_ but for OIDC providers. This allows to respect all the `AuthenticateOidcConfig`_
Properties as per CFN definition.

.. tip::

    We highly recommend that you store the OIDC details into a secret in secrets manager!

.. hint::

    For both AuthenticateCognitoConfig and AuthenticateOidcConfig, the rules defined in `access` will be set to come **after**
    the authenticate action.


Examples
========

.. literalinclude:: ../../../use-cases/elbv2/create_only.yml
    :language: yaml


.. code-block:: yaml
    :caption: ELBv2 with

    x-elbv2:
      authLb:
        Properties:
          Scheme: internet-facing
          Type: application
        Settings: {}
        Listeners:
          - Port: 8080
            Protocol: HTTP
            Targets:
              - name: app03:app03
                access: /
          - Port: 8081
            Protocol: HTTP
            Targets:
              - name: app03:app03
                access: /
                AuthenticateOidcConfig:
                  Issuer: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:Issuer}}"
                  AuthorizationEndpoint: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:AuthorizationEndpoint}}"
                  TokenEndpoint: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:TokenEndpoint}}"
                  UserInfoEndpoint: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:UserInfoEndpoint}}"
                  ClientId: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:ClientId}}"
                  ClientSecret: "{{resolve:secretsmanager:/oidc/azuread/app001:SecretString:ClientSecret}}"
                  SessionCookieName: "my-cookie"
                  SessionTimeout: 3600
                  Scope: "email"
                  AuthenticationRequestExtraParams":
                    display": "page"
                    prompt": "login"
                  OnUnauthenticatedRequest: "deny"
        Services:
          - name: app03:app03
            port: 5000
            healthcheck: 5000:HTTP:7:2:15:5
            protocol: HTTP


JSON Schema
============

Model
-------

.. jsonschema:: ../../../ecs_composex/elbv2/x-elbv2.spec.json

Definition
------------

.. literalinclude:: ../../../ecs_composex/elbv2/x-elbv2.spec.json

Test files
===========

You can find the test files `here <https://github.com/compose-x/ecs_composex/tree/main/use-cases/elbv2>`__ to use
as reference for your use-case.


.. _LB Attributes: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html#cfn-elasticloadbalancingv2-loadbalancer-loadbalancerattributes
.. _Scheme: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html#cfn-elasticloadbalancingv2-loadbalancer-scheme
.. _Type: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html#cfn-elasticloadbalancingv2-loadbalancer-type
.. _Target Group: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-targetgroup.html
.. _Listener definition: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-listener.html
.. _AuthenticateCognitoConfig : https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-elasticloadbalancingv2-listenerrule-authenticatecognitoconfig.html#cfn-elasticloadbalancingv2-listenerrule-authenticatecognitoconfig-userpoolarn
.. _AuthenticateOidcConfig: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-elasticloadbalancingv2-listenerrule-authenticateoidcconfig.html
.. _LoadBalancerAttributes: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html#cfn-elasticloadbalancingv2-loadbalancer-loadbalancerattributes
.. _Tags: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-loadbalancer.html#cfn-elasticloadbalancingv2-loadbalancer-tags
.. _x-elbv2 JSON Schema Definition: https://github.com/compose-x/ecs_composex_specs/blob/main/ecs_composex_specs/x-elbv2.spec.json
.. _Ingress JSON Schema definition: https://github.com/compose-x/ecs_composex_specs/blob/main/ecs_composex_specs/ingress.spec.json
.. _CFN TargetGroupAttributes: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-elasticloadbalancingv2-targetgroup.html#cfn-elasticloadbalancingv2-targetgroup-targetgroupattributes
.. _Target Group Attributes: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-properties-elasticloadbalancingv2-targetgroup-targetgroupattribute.html#aws-properties-elasticloadbalancingv2-targetgroup-targetgroupattribute-properties

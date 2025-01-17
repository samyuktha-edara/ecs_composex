
.. meta::
    :description: ECS Compose-X advanced network syntax reference
    :keywords: AWS, AWS ECS, Docker, Compose, docker-compose, networking, subnets, vpc, cloudmap

.. _x_configs_network_syntax:

==================
services.x-network
==================

.. code-block:: yaml

    services:
      serviceA:
        x-network:
          AssignPublicIp: bool
          Ingress: {}
          x-cloudmap: {}

.. _public_eip_for_service_option:

AssignPublicIp
===============

This flag allows to assign an Elastic IP to the container when using ``awsvpc`` networking mode.

.. hint::

    Make sure to set your service to be in a subnet that use an ``Internet Gateway``, such as ``PublicSubnets``, otherwise
    routing won't work.

.. tip::

    See :ref:`how_to_service_public_eip` to have a full example of how to implement this feature.

.. _services_ingress_syntax_reference:

.. tip::

    To select which subnets to place the services, see :ref:`compose_networks_syntax_reference`


Ingress
======================

This allows you to define specific ingress control from external sources to your environment. For example, if you have
to whitelist IP addresses that are to be allowed communication to the services, you can list these, and indicate their
name which will be shown in the EC2 security group description of the ingress rule.

Ingress Syntax reference
-----------------------------

.. code-block:: yaml

    Ingress:
      ExtSources: []
      AwsSources: []
      Myself: True/False

.. code-block:: yaml
    :caption: Ingress Example

    services:
      app01:
        x-network:
          Ingress:
            ExtSources:
              - IPv4: 0.0.0.0/0
                Name: all
              - IPv4: 1.1.1.1/32
                Source_name: CloudFlareDNS
            AwsSources:
              - Type: SecurityGroup
                Id: sg-abcd
              - Type: PrefixList
                Id: pl-abcd
            Myself: True/False

.. note::

    Future feature is to allow to input a security group ID and the remote account ID to allow ingress traffic from
    a security group owned by another of your account (or 3rd party).

.. hint::

    The protocol is automatically detected based on the port definition.
    By default, it is TCP

.. hint::

    To see details about the Ingress for Load Balancers, refer to :ref:`load_balancers_ingress_syntax_ref`


.. hint::

    When using an ALB, you do not need to define that ALB security group etc., all inbound rules will be defined automatically
    to allow the ALB to communicate with your service!

x-cloudmap
-----------

Refer to :ref:`cloudmap_reference_syntax` for more details on how to use it.

Map VPC subnets to docker-compose networks
===========================================

.. code-block:: yaml
    :caption: AWS VPC to network mapping

    networks:
      internal:
        x-vpc: InteralCustomSubnets

    x-vpc:
      VpcId:
        Tags: []
      AppSubnets:
        Tags: []
      PublicSubnets:
        Tags: []
      StorageSubnets:
        Tags: []
      InteralCustomSubnets:
        Tags: []

    services:
      serviceA:
        networks: [internal]


JSON Schema
============

Model
------

.. jsonschema:: ../../../../ecs_composex/specs/services.x-network.spec.json

Definition
-----------

.. literalinclude:: ../../../../ecs_composex/specs/services.x-network.spec.json

# Third-party notices

This file records direct runtime dependencies and externally owned names for the
public-release candidate. It does not replace the license text shipped by each
dependency.

## Model Context Protocol Python SDK

- Package: `mcp==1.26.0`
- Upstream: https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0
- License: MIT License
- Upstream notice: Copyright (c) 2024 Anthropic, PBC

The dependency's own distribution contains its complete MIT license text. Any
redistribution that includes the dependency must retain that copyright and
permission notice.

## Python and container base

The container build uses the pinned official Python 3.12 slim image. Python and
the Debian packages contained in that image remain under their respective
licenses. Redistributors of a built image should preserve the license metadata
and notices provided by that base image and its installed packages.

## Names and services

TAPD is an externally owned product and service name. This repository contains
an independent integration and does not include TAPD source code, credentials,
private API responses, or an endorsement by the service owner.

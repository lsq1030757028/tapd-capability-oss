"""Where the TAPD credential comes from, per transport.

One tool, two deployment shapes, and the credential rule is different in each:

* **stdio** — one process per person, started by that person's client. The
  process *is* the tenant boundary, so a process-level secret (environment
  variable, or a file outside the package) is the right answer.
* **streamable HTTP** — one process serving many people. There is no such thing
  as "the" credential; each call carries its own, in the request headers. The
  service holds none, stores none, and logs none.

The rule that makes the HTTP shape safe is the *absence* of a fallback: when a
request arrives without a credential the call is refused. Falling back to the
process environment would hand that caller whatever token the operator happened
to export — a lateral privilege escalation dressed up as a convenience.

This module is deliberately free of any MCP import so the rules can be tested
without a server, and so a wrong transport can never be papered over by a
half-initialized one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "streamable-http"

#: Accepted spellings on the command line and in the environment.
_TRANSPORT_ALIASES = {
    "stdio": TRANSPORT_STDIO,
    "http": TRANSPORT_HTTP,
    "streamable-http": TRANSPORT_HTTP,
    "streamablehttp": TRANSPORT_HTTP,
}

ENV_TRANSPORT = "TAPD_MCP_TRANSPORT"
ENV_HOST = "TAPD_MCP_HOST"
ENV_PORT = "TAPD_MCP_PORT"
ENV_TOKEN = "TAPD_ACCESS_TOKEN"

#: Bound wide because the intended home is a container behind a reverse proxy.
#: On a developer machine this is an exposed port — see README "安全姿态".
#: 默认只绑回环：本机开发是当前主场景，宽绑定必须显式（``--host 0.0.0.0`` 或
#: ``TAPD_MCP_HOST``）。回环绑定还会让 SDK 自动开启 DNS-rebinding 防护。
#: 云端部署时由反向代理终止 TLS 并限制来源，再显式放宽这一项。
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 3796

#: Standard first. The second exists for deployments whose gateway consumes
#: ``Authorization`` for its own auth and cannot pass it through.
AUTHORIZATION_HEADER = "authorization"
BEARER_SCHEME = "bearer"
ALTERNATE_HEADER = "x-tapd-access-token"


class CredentialError(RuntimeError):
    """No usable credential. Never carries a credential value in its message."""


@dataclass(frozen=True)
class Runtime:
    """Resolved transport settings. Defaults to stdio, always."""

    transport: str = TRANSPORT_STDIO
    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT

    @property
    def is_http(self) -> bool:
        return self.transport == TRANSPORT_HTTP

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"


def resolve_runtime(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Runtime:
    """Command line wins over environment; absence of both means stdio.

    Unknown flags are ignored rather than rejected: this module is imported by
    test runners whose argv belongs to them, and a stray flag must never change
    the transport. An *explicitly wrong* value is a different matter and raises.
    """
    environ = os.environ if environ is None else environ
    argv = [] if argv is None else list(argv)

    transport = _transport(_flag(argv, "--transport") or environ.get(ENV_TRANSPORT, ""))
    host = (
        _flag(argv, "--host") or environ.get(ENV_HOST, "") or DEFAULT_HTTP_HOST
    ).strip()
    port = _port(_flag(argv, "--port") or environ.get(ENV_PORT, ""))
    return Runtime(transport=transport, host=host, port=port)


def _flag(argv: list[str], name: str) -> str:
    """Read ``--name value`` or ``--name=value``. Credentials never come this way.

    Process listings are readable by other processes on the same host, so no
    flag in this tool ever carries a secret; only transport settings.
    """
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1].strip()
        if item.startswith(f"{name}="):
            return item.split("=", 1)[1].strip()
    return ""


def _transport(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return TRANSPORT_STDIO
    try:
        return _TRANSPORT_ALIASES[text]
    except KeyError:
        raise ValueError(
            f"未知的传输方式 {text!r}；可选：{sorted(set(_TRANSPORT_ALIASES))}"
        ) from None


def _port(value: str) -> int:
    text = (value or "").strip()
    if not text:
        return DEFAULT_HTTP_PORT
    try:
        port = int(text)
    except ValueError:
        raise ValueError(f"端口必须是整数，收到 {text!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"端口超出范围：{port}")
    return port


# --------------------------------------------------------------- credentials


def token_from_headers(headers: Mapping[str, str] | None) -> str:
    """The caller's own token, from this one request. No fallback exists.

    Refusal messages describe the *shape* that was expected and never echo any
    part of what arrived: a malformed ``Authorization`` header very often is the
    raw token with the scheme forgotten, and echoing it would copy a live
    credential into logs and transcripts.
    """
    lookup = _lower_keys(headers)

    raw = lookup.get(AUTHORIZATION_HEADER, "").strip()
    if raw:
        scheme, _, value = raw.partition(" ")
        if scheme.strip().lower() != BEARER_SCHEME:
            raise CredentialError(
                "Authorization 头不是 `Bearer <令牌>` 形式（内容不回显，避免把令牌写进日志）。"
                f"请改成 `Authorization: Bearer <你的 TAPD 令牌>`，或改用 {ALTERNATE_HEADER} 头。"
            )
        value = value.strip()
        if not value:
            raise CredentialError("Authorization: Bearer 后面没有令牌。")
        return value

    alternate = lookup.get(ALTERNATE_HEADER, "").strip()
    if alternate:
        return alternate

    raise CredentialError(
        "这次请求没有带 TAPD 令牌。HTTP 模式下服务本身不持有任何人的凭证，"
        "每次调用都必须自带，二选一："
        f"`Authorization: Bearer <你的 TAPD 令牌>`，或 `{ALTERNATE_HEADER}: <你的 TAPD 令牌>`。"
    )


def token_from_environment(
    environ: Mapping[str, str] | None = None, home: Path | None = None
) -> str:
    """Process-level credential for the stdio shape: env var, then a file.

    The file lives outside the package so copying the directory cannot carry it.
    """
    environ = os.environ if environ is None else environ
    token = environ.get(ENV_TOKEN, "").strip()
    if token:
        return token

    secret_file = _secret_file(home)
    try:
        token = secret_file.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise CredentialError(
            f"没有可用的 TAPD 令牌：设置环境变量 {ENV_TOKEN}，或把令牌写入 {secret_file}"
        ) from exc
    if not token:
        raise CredentialError(
            f"{secret_file} 是空的：请写入一行 TAPD 令牌，或设置 {ENV_TOKEN}"
        )
    return token


def _secret_file(home: Path | None) -> Path:
    if home is None:
        import store  # local import: keeps this module importable on its own

        home = store.home()
    return home / "credentials" / "tapd_access_token"


def _lower_keys(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}

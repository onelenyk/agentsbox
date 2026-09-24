"""HTTP CONNECT proxy for worker network egress control.

Workers run inside a Seatbelt sandbox that denies all network-outbound except
to localhost. This proxy listens on 127.0.0.1 on a random free port and
tunnels CONNECT requests to whitelisted hosts, answering 403 for everything else.

The worker gets HTTP_PROXY and HTTPS_PROXY pointing at this proxy, and NO_PROXY
set to localhost/127.0.0.1 so it can still reach the daemon's MCP endpoint.

Allows:
  - CONNECT requests only (HTTP tunneling), port 443 only
  - Host matching against an allowlist (exact match or *.domain suffix)
  - Plain HTTP requests get 403 immediately

Refuses:
  - All CONNECT requests to non-allowlisted hosts (403)
  - All other requests (403)
  - Each refused host is logged once at WARNING level
"""

import asyncio
import logging
import socket
from typing import Optional, Set

logger = logging.getLogger(__name__)

# Global state: the listening server and its port.
_server: Optional[asyncio.Server] = None
_port: Optional[int] = None
_refused_hosts: Set[str] = set()  # Track hosts we've already warned about

# Default allowlist: where workers can connect.
_DEFAULT_ALLOWLIST = [
    "api.anthropic.com",
    "claude.ai",
    "*.claude.ai",
    "statsig.anthropic.com",
    "sentry.io",
    "opencode.ai",
    "*.opencode.ai",
    "openrouter.ai",
    "api.deepseek.com",
    "models.dev",
    "registry.npmjs.org",
]


def allowed(host: str) -> bool:
    """Check if a host is on the allowlist.

    Supports exact match or *.domain suffix matching.
    """
    from core.config import get_config

    # Get the allowlist from config, or use the default.
    custom_list = get_config("sandbox.egress_allowlist")
    allowlist = custom_list if custom_list is not None else _DEFAULT_ALLOWLIST

    for entry in allowlist:
        if entry == host:
            # Exact match
            return True
        if entry.startswith("*."):
            # Suffix match: *.foo.com matches bar.foo.com
            domain = entry[2:]  # Remove *. prefix
            if host.endswith("." + domain):
                return True

    return False


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle one client connection to the proxy.

    Parses the CONNECT request, checks if the host is allowed, and either
    establishes a tunnel or returns 403.
    """
    try:
        # Read the first line to parse the CONNECT request
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
        request_line = line.decode("utf-8", errors="replace").strip()

        # Parse: CONNECT host:port HTTP/1.1
        if not request_line.startswith("CONNECT "):
            # Not a CONNECT request
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Extract host:port
        parts = request_line.split()
        if len(parts) < 2:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        hostport = parts[1]
        if ":" not in hostport:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        host, port_str = hostport.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Only allow port 443 (HTTPS)
        if port != 443:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Check if host is allowed
        if not allowed(host):
            if host not in _refused_hosts:
                _refused_hosts.add(host)
                logger.warning("egress refused: %s (not in allowlist)", host)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Read remaining headers until blank line
        while True:
            header_line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=5.0)
            if header_line.strip() == b"":
                break

        # Establish connection to upstream host
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=10.0
            )
        except Exception as e:
            logger.debug("could not connect to %s:%d: %s", host, port, e)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        # Send 200 response to client
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # Bidirectional tunnel: copy data between client and upstream
        async def copy_to_upstream():
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=300.0)
                    if not chunk:
                        break
                    upstream_writer.write(chunk)
                    await upstream_writer.drain()
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            finally:
                upstream_writer.close()
                await upstream_writer.wait_closed()

        async def copy_to_client():
            try:
                while True:
                    chunk = await asyncio.wait_for(upstream_reader.read(65536), timeout=300.0)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass
            finally:
                writer.close()

        # Run both directions concurrently
        await asyncio.gather(copy_to_upstream(), copy_to_client())

    except asyncio.TimeoutError:
        try:
            writer.write(b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    except Exception as e:
        logger.debug("connection handler error: %s", e)
        try:
            writer.close()
        except Exception:
            pass


async def start() -> int:
    """Start the egress proxy server. Returns the port it listens on.

    Idempotent: if already running, returns the existing port.
    """
    global _server, _port

    if _server is not None:
        assert _port is not None
        return _port

    # Find a free port by binding to port 0 (OS assigns a free port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    _, free_port = sock.getsockname()
    sock.close()

    # Start the server on that port
    _server = await asyncio.start_server(_handle_connection, "127.0.0.1", free_port)
    _port = free_port

    logger.info("egress proxy started on 127.0.0.1:%d", _port)

    # Run the server in the background
    asyncio.create_task(_server.serve_forever())

    return _port


async def stop() -> None:
    """Stop the egress proxy server. Idempotent."""
    global _server, _port

    if _server is None:
        return

    _server.close()
    await _server.wait_closed()
    _server = None
    _port = None
    _refused_hosts.clear()

    logger.info("egress proxy stopped")


async def worker_env() -> dict:
    """The proxy variables a worker (or the smoke test) gets when egress is
    on, starting the proxy if needed; {} when it is off. One helper for both
    launch paths, so the probe cannot see a different network than a run.
    NO_PROXY keeps the daemon's own MCP endpoint on 127.0.0.1 reachable."""
    from .config import get_config
    if not get_config("sandbox.egress", False):
        return {}
    url = f"http://127.0.0.1:{await start()}"
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}

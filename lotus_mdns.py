"""
mDNS / Bonjour support for LOTUS children (Phase D3).

Children announce themselves on `_lotus-agent._tcp.local.` with a TXT
record carrying the agent list and a token-hash fingerprint. Mother
browses the same service type and auto-registers any child whose token
fingerprint matches — rogue machines on the LAN can't join because they
don't know the shared token.

Design:
  - Service type: `_lotus-agent._tcp.local.` (IANA-style, underscore prefix)
  - Instance name: `<hostname>-<child_id>._lotus-agent._tcp.local.`
  - TXT keys (all bytes via zeroconf):
      agents      → "ParserAgent,ReviewAgent,ArchiverAgent"
      token_hash  → 16-hex-char sha256 prefix of the shared token
      child_id    → random 10-char hex id (same as in WS hello_ack)
      version     → "1"
  - Mother keeps a dict {instance_name → (host, port, agents, child_id)}
    and calls on_add / on_remove callbacks that the mother uses to
    register / deregister RemoteAgents.

Network: zeroconf sends UDP on 224.0.0.251:5353. No open ports required
beyond standard mDNS. Works on same-subnet LAN, doesn't cross routers
(which is what we want — children elsewhere should use LOTUS_CHILDREN
env var for explicit configuration).
"""

from __future__ import annotations

import hashlib
import socket
import threading
from typing import Callable, Optional

# zeroconf is imported lazily inside functions so machines without the
# lib can still run other parts of LOTUS (mother falls back to env-var
# configuration).


SERVICE_TYPE = "_lotus-agent._tcp.local."


def token_fingerprint(token: str) -> str:
    """16-hex-char prefix of SHA-256(token). Advertised in the mDNS TXT
    record so the mother can confirm a discovered child belongs to this
    LOTUS instance without exposing the token itself. For LAN-level
    trust this is good enough — a rogue needs the whole token to actually
    complete the WebSocket auth step when the mother connects."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _best_local_ip() -> str:
    """Pick a sensible outbound interface IP. Connecting a UDP socket to
    a public address doesn't actually send anything but reveals which
    interface the OS would route through — handy when the machine has
    multiple interfaces."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ── Child side: announce ─────────────────────────────────────────────

class ChildAnnouncer:
    """Registers this process as a LOTUS child on the local network.
    Keep the returned object alive for the lifetime of the child —
    destruction unregisters.

    The Zeroconf instance is OWNED by a dedicated thread so the library's
    own event loop doesn't collide with whatever asyncio loop the caller
    is running (important — lotus-child.py's main is `asyncio.run(...)`).
    """

    def __init__(self, port: int, agents: list, token: str,
                 child_id: str, hostname: Optional[str] = None):
        self.port = port
        self._agents = list(agents)
        self._token = token
        self._child_id = child_id
        self._hostname = hostname or socket.gethostname()
        self.ip = _best_local_ip()
        self.instance = (
            f"{self._hostname}-{child_id}.{SERVICE_TYPE}"
        )
        self._ready = threading.Event()
        self._stop  = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._owner_thread,
            name=f"mdns-child-{child_id}",
            daemon=True,
        )
        self._thread.start()
        # Wait up to 5s for registration to complete; surface the error
        # if it raised so the caller sees it instead of silent failure.
        if not self._ready.wait(timeout=5):
            raise RuntimeError("mDNS announcer didn't become ready in 5s")
        if self._error:
            raise self._error

    def _owner_thread(self) -> None:
        try:
            from zeroconf import Zeroconf, ServiceInfo
            zc = Zeroconf()
            props = {
                b"agents":     ",".join(self._agents).encode(),
                b"token_hash": token_fingerprint(self._token).encode(),
                b"child_id":   self._child_id.encode(),
                b"version":    b"1",
                b"hostname":   self._hostname.encode(),
            }
            info = ServiceInfo(
                type_=SERVICE_TYPE,
                name=self.instance,
                addresses=[socket.inet_aton(self.ip)],
                port=self.port,
                properties=props,
                server=f"{self._hostname}.local.",
            )
            zc.register_service(info)
        except BaseException as e:
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        # Block the thread until close() — zeroconf handles socket I/O in
        # its own sub-threads, we just own the lifecycle.
        self._stop.wait()
        try: zc.unregister_service(info)
        except Exception: pass
        try: zc.close()
        except Exception: pass

    def close(self) -> None:
        self._stop.set()


# ── Mother side: browse ──────────────────────────────────────────────

class ChildBrowser:
    """Watches the local network for LOTUS children and fires callbacks
    when their set changes.

    Callbacks run on the zeroconf thread; they should be cheap and do
    the heavy work (e.g. registering a RemoteAgent) on their own
    thread/queue if blocking.

    Filters out children whose advertised token_hash != our own — stops
    us from accidentally linking to somebody else's LOTUS instance on
    the same LAN.
    """

    def __init__(self, token: str,
                 on_add: Callable[[dict], None],
                 on_remove: Callable[[str], None]):
        from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
        self._zc = Zeroconf()
        self._on_add = on_add
        self._on_remove = on_remove
        self._expected_hash = token_fingerprint(token)
        # Cache of known instances — (instance_name → info_dict) so we
        # can surface the right data to on_remove.
        self._known: dict = {}
        self._lock = threading.Lock()

        def _handler(zeroconf, service_type, name, state_change, **_kw):
            # Zeroconf invokes the handler with kwargs; accept extras to
            # stay forward-compatible if the API adds more.
            if state_change == ServiceStateChange.Added or state_change == ServiceStateChange.Updated:
                info = zeroconf.get_service_info(service_type, name, timeout=3000)
                if not info: return
                props = {
                    k.decode(errors="replace"): v.decode(errors="replace") if isinstance(v, bytes) else v
                    for k, v in (info.properties or {}).items()
                }
                if props.get("token_hash") != self._expected_hash:
                    print(f"[mdns] ignoring {name} — token_hash mismatch "
                          f"(wrong LOTUS instance)")
                    return
                addresses = [socket.inet_ntoa(a) for a in (info.addresses or [])]
                host = addresses[0] if addresses else info.server
                record = {
                    "instance": name,
                    "host":     host,
                    "port":     info.port,
                    "agents":   (props.get("agents") or "").split(","),
                    "child_id": props.get("child_id", ""),
                    "hostname": props.get("hostname", ""),
                }
                with self._lock:
                    first_time = name not in self._known
                    self._known[name] = record
                if first_time:
                    try: self._on_add(record)
                    except Exception as e:
                        print(f"[mdns] on_add error: {e}")
            elif state_change == ServiceStateChange.Removed:
                with self._lock:
                    rec = self._known.pop(name, None)
                if rec:
                    try: self._on_remove(name)
                    except Exception as e:
                        print(f"[mdns] on_remove error: {e}")

        self._browser = ServiceBrowser(
            self._zc, SERVICE_TYPE, handlers=[_handler]
        )

    def known(self) -> list:
        with self._lock:
            return list(self._known.values())

    def close(self) -> None:
        try: self._browser.cancel()
        except Exception: pass
        try: self._zc.close()
        except Exception: pass

"""`python -m tesapi`: start the listener. `python -m tesapi token <name>`: make a token.

The listener refuses to start unless everything that decides what a task may touch
is set, the runner is one that can run the images listed, and, for a runner that
claims to keep steps off the network, one step has shown it does.
"""

import secrets
import sys

import uvicorn

import audit
from runner import ENFORCED, NetworkCheck, check_network, get_runner, require_network_isolation
from tesapi import config as settings
from tesapi.app import create_app
from tesapi.tasks import SOURCE


def new_token(name):
    if not settings.NAME_SHAPE.match(name):
        print(f"{name!r} is not a name: letters, digits, . _ - up to 64", file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(32)
    print("Give this token to the caller. It is not stored and cannot be shown again:")
    print(f"  {token}")
    print("Add this line to the file BIOCHEF_TESAPI_TOKENS names (mode 0600):")
    print(f"  {name} sha256:{settings.hash_token(token)}")
    return 0


def prepare(config, provider):
    """Everything the listener checks before it takes a request."""
    if provider.guarantees["network"] != ENFORCED and require_network_isolation():
        raise settings.ConfigError(
            f"BIOCHEF_REQUIRE_NETWORK_ISOLATION refuses the {provider.name} runner "
            f"here as it does for the agent: its network is "
            f"{provider.guarantees['network']}")
    for image in config.images:
        try:
            provider.with_image(image)
        except ValueError as refused:
            raise settings.ConfigError(f"BIOCHEF_TESAPI_IMAGES: {image}: {refused}") \
                from None


def start_checks(config, provider):
    def run():
        if not require_network_isolation():
            audit.record("node.runner.isolation_not_required", source=SOURCE,
                         runner=provider.name, network=provider.guarantees["network"])
        result = check_network(provider, root=config.run_root or None,
                               timeout_s=config.timeout_s)
        if result is not None:
            audit.record("node.runner.network_check", source=SOURCE,
                         runner=provider.name, ok=result.ok, detail=result.detail)
            if not result.ok:
                raise RuntimeError(f"the network check failed: {result.detail}")
        audit.record("tes.start", source=SOURCE, runner=provider.name,
                     guarantees=provider.guarantees, images=list(config.images),
                     dirs=list(config.dirs), max_concurrent=config.max_concurrent,
                     max_queued=config.max_queued)
    return run


def main(argv):
    if argv[:1] == ["token"]:
        return new_token(argv[1]) if len(argv) == 2 else 2
    try:
        config = settings.load()
        import os
        provider = get_runner(os.getenv("BIOCHEF_RUNNER", "subprocess"))
        prepare(config, provider)
    except (settings.ConfigError, ValueError) as refused:
        print(refused, file=sys.stderr)
        return 1
    app = create_app(config, provider, start_checks(config, provider))
    options = {}
    if config.tls_cert:
        options = {"ssl_certfile": config.tls_cert, "ssl_keyfile": config.tls_key}
    uvicorn.run(app, host=config.host, port=config.port, proxy_headers=False,
                server_header=False, **options)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

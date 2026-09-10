"""Read one secret from Secret Manager, at call time. Never fall back to anything.

WHY THIS IS A MODULE AND NOT A THIRD COPY. campaign.api_key() grew its own reader for the Brevo
key; the seam now needs two more (`relay_secret`, `press_endpoint_secret`). Three private readers
is three chances for one of them to answer None instead of refusing, and a credential that returns
None turns a guard into a no-op without a line in any log. So there is one reader, it raises, and
the caller decides what a missing secret means for its own job.

A CACHE, NOT A FALLBACK. Values are cached per process because Cloud Run reuses an instance across
requests and Secret Manager is a network call. The cache never holds a failure - a secret that
could not be read this minute is re-read next minute, because the usual cause is an IAM binding
somebody is in the middle of adding.
"""
import logging
import os

log = logging.getLogger("gsecret")

PROJECT = os.environ.get("SECRET_PROJECT") or os.environ.get(
    "BQ_PROJECT", "jaunais-za-aizv04022026")

_cache = {}


class SecretUnavailable(RuntimeError):
    """The secret cannot be read, and it is named. Fail closed; never continue without it."""


def read(secret_id: str, env_override: str = None) -> str:
    """Return the secret's latest version as text, or raise SecretUnavailable.

    env_override names an environment variable that may carry the value instead. It exists for the
    same reason campaign.api_key() accepts BREVO_API_KEY inline: a Cloud Run env var is protected by
    the same IAM as the deployment itself, and requiring Secret Manager for a local run would mean
    the refusal paths could only ever be exercised in production. It is a place the value may LIVE,
    never a place an empty value becomes acceptable - an empty override falls through to Secret
    Manager and, failing that, raises.
    """
    if env_override:
        inline = (os.environ.get(env_override) or "").strip()
        if inline:
            return inline
    if secret_id in _cache:
        return _cache[secret_id]
    name = f"projects/{PROJECT}/secrets/{secret_id}/versions/latest"
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        value = client.access_secret_version(
            request={"name": name}).payload.data.decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        raise SecretUnavailable(
            f"cannot read {name}: {e!r}. Either the secret does not exist yet or this service "
            f"account lacks roles/secretmanager.secretAccessor on it. Refusing rather than "
            f"continuing without it.") from e
    if not value:
        raise SecretUnavailable(f"{name} is readable but empty, which is not a credential")
    _cache[secret_id] = value
    return value

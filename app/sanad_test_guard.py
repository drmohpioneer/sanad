"""Process-wide hermetic boundaries for Sanad's unittest suite.

This module is installed by ``sitecustomize`` before test modules import the
application.  ``tests.__init__`` installs it again as a fail-safe and, more
importantly, rejects a late install if the application already owns a real
cached cloud client.

The current-process network contract covers DNS lookup, new TCP connections,
and UDP sends through Python socket entry points and matching CPython audit
events.  It also blocks the pinned grpcio sync/async channel factories and the
installed generated Firestore, Cloud Tasks, and Cloud Storage gRPC transport
constructors before native C-core channel creation.  It also blocks the pinned
Google Operations REST/client constructors, Firestore/Cloud Tasks REST
transports, high-level and generated Cloud clients, GenAI Base/GAOS clients,
and shared Cloud service-account factories.  The audit hook catches socket
aliases imported before the monkeypatches.

Already-connected inherited sockets, arbitrary networking inside otherwise
permitted child processes, and native extensions that bypass the specifically
guarded gRPC entry points are outside this boundary.  Sanad's required ffmpeg
child therefore remains available; tests must not use a generic child or raw
native syscall as a network escape.
"""

from __future__ import annotations

import atexit
import asyncio
import importlib
import importlib.metadata
import importlib.util
import inspect
import os
import socket
import sys
import threading
import traceback
import unittest
from types import SimpleNamespace
from typing import Any, NoReturn


TEST_PROJECT = "sanad-hermetic-test-only"
TEST_CONFIG = "/__sanad_hermetic_test__/no-gcloud-config"
TEST_CREDENTIALS = "/__sanad_hermetic_test__/no-credentials.json"
PINNED_GOOGLE_AUTH_VERSION = "2.57.0"
PINNED_GRPCIO_VERSION = "1.83.1"
PINNED_API_CORE_VERSION = "2.34.0"
PINNED_CLOUD_CORE_VERSION = "2.7.0"
PINNED_GENAI_VERSION = "2.20.0"
PINNED_FIRESTORE_VERSION = "2.29.0"
PINNED_TASKS_VERSION = "2.24.0"
PINNED_STORAGE_VERSION = "3.13.1"


class HermeticTestViolation(BaseException):
    """A test attempted external work without replacing the boundary."""


class HermeticBootstrapError(RuntimeError):
    """The pinned hermetic boundary no longer matches its installed SDK."""


def _refuse(boundary: str) -> NoReturn:
    violation = HermeticTestViolation(
        "hermetic unittest boundary: unmocked "
        f"{boundary}; replace it with an in-memory double"
    )
    _record_violation(violation)
    raise violation


def _block(boundary: str):
    def blocked(*_: Any, **__: Any) -> NoReturn:
        _refuse(boundary)

    blocked.__name__ = "blocked_by_sanad_hermetic_tests"
    return blocked


def _async_block(boundary: str):
    async def blocked(*_: Any, **__: Any) -> NoReturn:
        _refuse(boundary)

    blocked.__name__ = "blocked_by_sanad_hermetic_tests"
    return blocked


class _BlockedSurface:
    def __getattr__(self, name: str) -> NoReturn:
        _refuse(f"GenAI client property {name!r}")


class _BlockedModels(_BlockedSurface):
    def generate_content(self, *_: Any, **__: Any) -> NoReturn:
        _refuse("GenAI request")


class _BlockedAsyncModels(_BlockedSurface):
    async def generate_content(self, *_: Any, **__: Any) -> NoReturn:
        _refuse("GenAI request")


class _BlockedAio(_BlockedSurface):
    def __init__(self) -> None:
        self.models = _BlockedAsyncModels()


class BlockedGenAIClient(_BlockedSurface):
    """An inert client preserving the suite's existing model mock seam."""

    _sanad_hermetic = True

    def __init__(self, *_: Any, **__: Any) -> None:
        self.models = _BlockedModels()
        self.aio = _BlockedAio()


class BlockedGrpcChannel:
    """Type-shaped native-channel sentinel for pinned grpcio entry points."""

    def __new__(cls, *_: Any, **__: Any) -> NoReturn:
        _refuse("gRPC native channel construction")


def is_blocked_genai_client(value: Any) -> bool:
    return isinstance(value, BlockedGenAIClient)


_installed = False
_install_source = ""
_background_lock = threading.Lock()
_unacknowledged: dict[int, HermeticTestViolation] = {}


def guards_installed() -> bool:
    return _installed


def install_source() -> str:
    return _install_source


def _assert_pinned_distribution(distribution: str, expected: str) -> None:
    try:
        actual = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise HermeticBootstrapError(
            f"hermetic unittest bootstrap requires {distribution}=={expected}, "
            f"but {distribution} is not installed"
        ) from exc
    if actual != expected:
        raise HermeticBootstrapError(
            f"hermetic unittest bootstrap was audited for {distribution}=="
            f"{expected}, but found {actual}; update the pin, "
            "guard inventory, and independent canaries together"
        )


def _assert_pinned_dependencies() -> None:
    for distribution, expected in (
        ("google-auth", PINNED_GOOGLE_AUTH_VERSION),
        ("grpcio", PINNED_GRPCIO_VERSION),
        ("google-api-core", PINNED_API_CORE_VERSION),
        ("google-cloud-core", PINNED_CLOUD_CORE_VERSION),
        ("google-genai", PINNED_GENAI_VERSION),
        ("google-cloud-firestore", PINNED_FIRESTORE_VERSION),
        ("google-cloud-tasks", PINNED_TASKS_VERSION),
        ("google-cloud-storage", PINNED_STORAGE_VERSION),
    ):
        _assert_pinned_distribution(distribution, expected)


def _assert_clients_were_not_constructed_first() -> None:
    media = sys.modules.get("core.media")
    if media is not None and hasattr(media, "client"):
        client = getattr(media, "client")
        if not is_blocked_genai_client(client):
            _refuse(
                "late test bootstrap after core.media constructed a real "
                "GenAI client"
            )

    store = sys.modules.get("core.store")
    if store is not None and getattr(store, "_db", None) is not None:
        _refuse(
            "late test bootstrap after core.store cached a real Firestore client"
        )


def _assert_external_sdk_was_not_imported_first() -> None:
    prefixes = (
        "google.auth",
        "google.cloud",
        "google.genai",
        "google.oauth2",
        "grpc",
    )
    imported = sorted(
        name for name in sys.modules if name in prefixes or name.startswith(prefixes)
    )
    if imported:
        sample = ", ".join(imported[:3])
        _refuse(f"late test bootstrap after external SDK import ({sample})")


def _neutralize_external_configuration() -> None:
    os.environ["SANAD_TEST_MODE"] = "1"
    for name in (
        "GOOGLE_CLOUD_PROJECT",
        "GCLOUD_PROJECT",
        "CLOUDSDK_CORE_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
    ):
        os.environ[name] = TEST_PROJECT
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = TEST_CREDENTIALS
    os.environ["CLOUDSDK_CONFIG"] = TEST_CONFIG
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    os.environ["GOOGLE_CLOUD_LOCATION"] = "test-only"

    # These values are consumed while google-auth modules import.  Clear or
    # force them to their inert modes before the first Google module exists in
    # this process; patching callables afterwards would already be too late.
    for name in (
        "GOOGLE_API_CERTIFICATE_CONFIG",
        "CLOUDSDK_CONTEXT_AWARE_CERTIFICATE_CONFIG_FILE_PATH",
        "GOOGLE_EXTERNAL_ACCOUNT_OUTPUT_FILE",
        "GOOGLE_EXTERNAL_ACCOUNT_AUDIENCE",
        "GOOGLE_EXTERNAL_ACCOUNT_TOKEN_TYPE",
        "GOOGLE_EXTERNAL_ACCOUNT_ID",
        "GOOGLE_EXTERNAL_ACCOUNT_IMPERSONATED_EMAIL",
        "GOOGLE_AUTH_WEBAUTHN_PLUGIN",
        "AWS_ACCESS_KEY_ID",
        "AWS_ACCESS_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECRET_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_STS",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_METADATA_SERVICE_TIMEOUT",
        "AWS_METADATA_SERVICE_NUM_ATTEMPTS",
    ):
        os.environ.pop(name, None)
    os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "false"
    os.environ["CLOUDSDK_CONTEXT_AWARE_USE_CLIENT_CERTIFICATE"] = "false"
    os.environ["GOOGLE_API_USE_MTLS_ENDPOINT"] = "never"
    os.environ["GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES"] = "false"
    os.environ["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] = "0"
    os.environ["GOOGLE_EXTERNAL_ACCOUNT_INTERACTIVE"] = "0"
    os.environ["GOOGLE_EXTERNAL_ACCOUNT_REVOKE"] = "0"
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["NO_GCE_CHECK"] = "true"
    os.environ["GCE_METADATA_MTLS_MODE"] = "none"
    os.environ["GCE_METADATA_HOST"] = "metadata.invalid"
    os.environ["GCE_METADATA_ROOT"] = "metadata.invalid"
    os.environ["GCE_METADATA_IP"] = "0.0.0.0"
    os.environ["GCE_METADATA_TIMEOUT"] = "1"
    os.environ["GCE_METADATA_DETECT_RETRIES"] = "1"
    os.environ["APPENGINE_RUNTIME"] = ""
    for name in (
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "LABS_BUCKET",
        "SERVICE_URL",
        "SANAD_SA",
        "SANAD_BOT_TOKEN",
    ):
        os.environ[name] = ""


_NETWORK_AUDIT_EVENTS = frozenset(
    {
        "http.client.connect",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.sendmsg",
        "socket.sendto",
        "urllib.Request",
    }
)


def _audit(event: str, _args: tuple[Any, ...]) -> None:
    if event in _NETWORK_AUDIT_EVENTS:
        _refuse(f"current-process network operation ({event})")


def _install_network_guards() -> None:
    sys.addaudithook(_audit)
    for name in (
        "create_connection",
        "getaddrinfo",
        "getfqdn",
        "gethostbyaddr",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
    ):
        setattr(socket, name, _block(f"network or DNS call socket.{name}"))
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(
                socket.socket,
                name,
                _block(f"network call socket.socket.{name}"),
            )


def _required_module(name: str):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise HermeticBootstrapError(
            "pinned hermetic seam module "
            f"{name!r} is unavailable under the audited dependency versions"
        ) from exc


def _required_member(owner: Any, name: str, label: str) -> Any:
    try:
        inspect.getattr_static(owner, name)
    except AttributeError as exc:
        raise HermeticBootstrapError(
            "pinned hermetic seam "
            f"{label}.{name} is missing under the audited dependency versions"
        ) from exc
    return getattr(owner, name)


def _required_class(module: Any, class_name: str, module_name: str) -> type:
    value = _required_member(module, class_name, module_name)
    if not isinstance(value, type):
        raise HermeticBootstrapError(
            f"hermetic seam {module_name}.{class_name} is no longer a class"
        )
    return value


def _patch_module_functions(
    modules: dict[str, Any], specs: tuple[tuple[str, tuple[str, ...]], ...], boundary: str
) -> None:
    blocked = _block(boundary)
    for module_name, names in specs:
        module = modules[module_name]
        for name in names:
            value = _required_member(module, name, module_name)
            if not callable(value):
                raise HermeticBootstrapError(
                    f"hermetic seam {module_name}.{name} is no longer callable"
                )
            setattr(module, name, blocked)


def _patch_async_module_functions(
    modules: dict[str, Any], specs: tuple[tuple[str, tuple[str, ...]], ...], boundary: str
) -> None:
    blocked = _async_block(boundary)
    for module_name, names in specs:
        module = modules[module_name]
        for name in names:
            value = _required_member(module, name, module_name)
            if not inspect.iscoroutinefunction(value):
                raise HermeticBootstrapError(
                    f"hermetic seam {module_name}.{name} is no longer async"
                )
            setattr(module, name, blocked)


def _patch_class_members(
    modules: dict[str, Any],
    specs: tuple[tuple[str, str, tuple[str, ...]], ...],
    boundary: str,
    descriptor_kind: str,
) -> None:
    blocked = _block(boundary)
    for module_name, class_name, names in specs:
        credential_class = _required_class(
            modules[module_name], class_name, module_name
        )
        label = f"{module_name}.{class_name}"
        for name in names:
            descriptor = inspect.getattr_static(credential_class, name, None)
            if descriptor is None:
                raise HermeticBootstrapError(f"hermetic seam {label}.{name} is missing")
            if descriptor_kind == "classmethod":
                if not isinstance(descriptor, classmethod):
                    raise HermeticBootstrapError(
                        f"hermetic seam {label}.{name} is no longer a classmethod"
                    )
                replacement: Any = classmethod(blocked)
            elif descriptor_kind == "property":
                if not isinstance(descriptor, property):
                    raise HermeticBootstrapError(
                        f"hermetic seam {label}.{name} is no longer a property"
                    )
                replacement = property(blocked)
            else:
                if not callable(getattr(credential_class, name, None)):
                    raise HermeticBootstrapError(
                        f"hermetic seam {label}.{name} is no longer callable"
                    )
                replacement = blocked
            setattr(credential_class, name, replacement)


def _install_credential_guards() -> None:
    module_functions = (
        (
            "google.auth",
            ("default", "load_credentials_from_dict", "load_credentials_from_file"),
        ),
        (
            "google.auth._default",
            (
                "default",
                "get_api_key_credentials",
                "load_credentials_from_dict",
                "load_credentials_from_file",
                "_get_explicit_environ_credentials",
                "_get_authorized_user_credentials",
                "_get_external_account_authorized_user_credentials",
                "_get_external_account_credentials",
                "_get_gae_credentials",
                "_get_gce_credentials",
                "_get_gcloud_sdk_credentials",
                "_get_gdch_service_account_credentials",
                "_get_impersonated_service_account_credentials",
                "_get_service_account_credentials",
                "_load_credentials_from_info",
            ),
        ),
        (
            "google.auth._default_async",
            (
                "default_async",
                "load_credentials_from_file",
                "_get_explicit_environ_credentials",
                "_get_gae_credentials",
                "_get_gce_credentials",
                "_get_gcloud_sdk_credentials",
            ),
        ),
        ("google.auth._service_account_info", ("from_dict", "from_filename")),
        (
            "google.oauth2.id_token",
            ("fetch_id_token", "fetch_id_token_credentials"),
        ),
        (
            "google.auth._cloud_sdk",
            (
                "_run_subprocess_ignore_stderr",
                "get_application_default_credentials_path",
                "get_auth_access_token",
                "get_config_path",
                "get_project_id",
            ),
        ),
        (
            "google.auth.compute_engine",
            ("detect_gce_residency_linux",),
        ),
        (
            "google.auth.compute_engine._metadata",
            (
                "detect_gce_residency_linux",
                "get",
                "get_project_id",
                "get_service_account_info",
                "get_service_account_token",
                "get_universe_domain",
                "is_on_gce",
                "ping",
                "_prepare_request_for_mds",
            ),
        ),
        (
            "google.auth._agent_identity_utils",
            (
                "_get_cert_path_with_optional_polling",
                "_is_certificate_file_ready",
                "_parse_cert_path_from_config",
                "get_agent_identity_certificate_path",
                "get_and_parse_agent_identity_certificate",
                "get_cached_cert_fingerprint",
                "parse_certificate",
            ),
        ),
        (
            "google.auth.transport._mtls_helper",
            (
                "_can_read",
                "_check_config_path",
                "_encrypt_key_if_plaintext",
                "_get_cert_config_path",
                "_get_workload_cert_and_key",
                "_get_workload_cert_and_key_paths",
                "_load_json_file",
                "_memfd_cert_key_paths",
                "_read_cert_and_key_files",
                "_read_cert_file",
                "_read_key_file",
                "_run_cert_provider_command",
                "_secure_wipe_and_remove",
                "_tempfile_cert_key_paths",
                "_write_secure_tempfile",
                "call_client_cert_callback",
                "check_parameters_for_unauthorized_response",
                "check_use_client_cert",
                "decrypt_private_key",
                "get_client_cert_and_key",
                "get_client_ssl_credentials",
                "secure_cert_key_paths",
            ),
        ),
        (
            "google.auth.transport.mtls",
            (
                "_load_client_cert_into_context",
                "default_client_cert_source",
                "default_client_encrypted_cert_source",
                "get_default_ssl_context",
                "has_default_client_cert_source",
                "load_default_client_cert",
                "should_use_client_cert",
            ),
        ),
        (
            "google.auth.aio.transport.mtls",
            (
                "default_client_cert_source",
                "make_client_cert_ssl_context",
                "secure_cert_key_paths",
            ),
        ),
        (
            "google.auth.transport._custom_tls_signer",
            (
                "get_cert",
                "get_sign_callback",
                "load_offload_lib",
                "load_provider_lib",
                "load_signer_lib",
            ),
        ),
        ("google.auth.app_engine", ("get_project_id",)),
    )

    async_module_functions = (
        ("google.oauth2._id_token_async", ("fetch_id_token",)),
        (
            "google.auth.aio.transport.mtls",
            (
                "_run_in_executor",
                "get_client_cert_and_key",
                "get_client_ssl_credentials",
            ),
        ),
    )

    class_factories = (
        (
            "google.oauth2.service_account",
            "Credentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.oauth2.service_account",
            "IDTokenCredentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.oauth2._service_account_async",
            "Credentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.oauth2._service_account_async",
            "IDTokenCredentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.oauth2.credentials",
            "Credentials",
            ("from_authorized_user_file", "from_authorized_user_info"),
        ),
        (
            "google.oauth2._credentials_async",
            "Credentials",
            ("from_authorized_user_file", "from_authorized_user_info"),
        ),
        (
            "google.auth.jwt",
            "Credentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.auth.jwt",
            "OnDemandCredentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.auth._jwt_async",
            "Credentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.auth._jwt_async",
            "OnDemandCredentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.auth.identity_pool",
            "Credentials",
            ("from_file", "from_info"),
        ),
        (
            "google.auth.external_account",
            "Credentials",
            ("from_file", "from_info"),
        ),
        (
            "google.auth.external_account_authorized_user",
            "Credentials",
            ("from_file", "from_info"),
        ),
        ("google.auth.aws", "Credentials", ("from_file", "from_info")),
        ("google.auth.pluggable", "Credentials", ("from_file", "from_info")),
        (
            "google.oauth2.gdch_credentials",
            "ServiceAccountCredentials",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.auth.impersonated_credentials",
            "Credentials",
            ("from_impersonated_service_account_info",),
        ),
        (
            "google.auth.crypt.base",
            "FromServiceAccountMixin",
            ("from_service_account_file", "from_service_account_info"),
        ),
        (
            "google.cloud.client",
            "_ClientFactoryMixin",
            ("from_service_account_json", "from_service_account_info"),
        ),
        ("google.auth.crypt.rsa", "RSASigner", ("from_string",)),
        ("google.auth.crypt._cryptography_rsa", "RSASigner", ("from_string",)),
        ("google.auth.crypt.es", "EsSigner", ("from_string",)),
        ("google.auth.crypt.es256", "ES256Signer", ("from_string",)),
    )

    instance_methods = (
        (
            "google.oauth2.credentials",
            "UserAccessTokenCredentials",
            ("before_request", "refresh"),
        ),
        (
            "google.oauth2._credentials_async",
            "UserAccessTokenCredentials",
            ("before_request", "refresh"),
        ),
        ("google.auth.aws", "Credentials", ("retrieve_subject_token",)),
        (
            "google.auth.aws",
            "_DefaultAwsSecurityCredentialsSupplier",
            (
                "_get_imdsv2_session_token",
                "_get_metadata_role_name",
                "_get_metadata_security_credentials",
                "get_aws_region",
                "get_aws_security_credentials",
            ),
        ),
        (
            "google.auth.identity_pool",
            "Credentials",
            (
                "_get_cert_bytes",
                "_get_mtls_cert_and_key_paths",
                "refresh",
                "retrieve_subject_token",
            ),
        ),
        ("google.auth.identity_pool", "_FileSupplier", ("get_subject_token",)),
        ("google.auth.identity_pool", "_UrlSupplier", ("get_subject_token",)),
        (
            "google.auth.identity_pool",
            "_X509Supplier",
            ("_read_trust_chain", "get_subject_token"),
        ),
        (
            "google.auth.pluggable",
            "Credentials",
            ("retrieve_subject_token", "revoke"),
        ),
        (
            "google.auth.compute_engine.credentials",
            "Credentials",
            (
                "_build_regional_access_boundary_lookup_url",
                "_perform_refresh_token",
                "_retrieve_info",
            ),
        ),
        (
            "google.auth.compute_engine.credentials",
            "IDTokenCredentials",
            ("__init__", "_call_metadata_identity_endpoint", "refresh"),
        ),
        ("google.auth.app_engine", "Signer", ("sign",)),
        (
            "google.auth.app_engine",
            "Credentials",
            ("__init__", "refresh", "sign_bytes"),
        ),
        (
            "google.auth.compute_engine._mtls",
            "MdsMtlsAdapter",
            ("__init__", "send"),
        ),
        (
            "google.auth.transport._custom_tls_signer",
            "CustomTlsSigner",
            ("attach_to_ssl_context", "load_libraries", "set_up_custom_key"),
        ),
        (
            "google.auth.crypt._cryptography_rsa",
            "RSASigner",
            ("__getstate__", "__setstate__"),
        ),
        (
            "google.auth.crypt.es",
            "EsSigner",
            ("__getstate__", "__setstate__"),
        ),
        (
            "google.oauth2.webauthn_handler",
            "PluginHandler",
            ("_call_plugin", "get"),
        ),
    )

    properties = (
        ("google.auth.app_engine", "Credentials", ("service_account_email",)),
        (
            "google.auth.compute_engine.credentials",
            "Credentials",
            ("universe_domain",),
        ),
    )

    optional_factories: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    if importlib.util.find_spec("rsa") is not None:
        optional_factories = (
            ("google.auth.crypt._python_rsa", "RSASigner", ("from_string",)),
        )

    compute_mtls_functions = (
        (
            "google.auth.compute_engine._mtls",
            ("_certs_exist", "should_use_mds_mtls"),
        ),
    )

    all_specs = (
        module_functions
        + async_module_functions
        + compute_mtls_functions
        + tuple((module, ()) for module, _, _ in class_factories)
        + tuple((module, ()) for module, _, _ in optional_factories)
        + tuple((module, ()) for module, _, _ in instance_methods)
        + tuple((module, ()) for module, _, _ in properties)
    )
    modules = {name: _required_module(name) for name, _ in all_specs}

    cloud_factory_mixin = _required_class(
        modules["google.cloud.client"],
        "_ClientFactoryMixin",
        "google.cloud.client",
    )
    for module_name, class_name in (
        ("google.cloud.storage.client", "Client"),
        ("google.cloud.firestore_v1.client", "Client"),
    ):
        client_module = _required_module(module_name)
        client_class = _required_class(client_module, class_name, module_name)
        for method_name in (
            "from_service_account_json",
            "from_service_account_info",
        ):
            defining_descriptor = inspect.getattr_static(
                cloud_factory_mixin, method_name
            )
            inherited_descriptor = inspect.getattr_static(client_class, method_name)
            if inherited_descriptor is not defining_descriptor:
                raise HermeticBootstrapError(
                    f"pinned credential factory {module_name}.{class_name}."
                    f"{method_name} no longer inherits google.cloud.client."
                    f"_ClientFactoryMixin.{method_name}"
                )

    boundary = "Google credential, identity, metadata, or private-key acquisition"
    _patch_module_functions(modules, module_functions, boundary)
    _patch_async_module_functions(modules, async_module_functions, boundary)
    _patch_module_functions(modules, compute_mtls_functions, boundary)
    _patch_class_members(
        modules,
        class_factories + optional_factories,
        boundary,
        "classmethod",
    )
    _patch_class_members(modules, instance_methods, boundary, "method")
    _patch_class_members(modules, properties, boundary, "property")


def _require_same_alias(
    alias_owner: Any,
    defining_owner: Any,
    name: str,
    alias_label: str,
    defining_label: str,
) -> None:
    alias = _required_member(alias_owner, name, alias_label)
    defining = _required_member(defining_owner, name, defining_label)
    if alias is not defining:
        raise HermeticBootstrapError(
            f"pinned hermetic alias {alias_label}.{name} no longer references "
            f"{defining_label}.{name}"
        )


def _require_class_aliases(
    modules: dict[str, Any],
    defining_module: str,
    class_name: str,
    aliases: tuple[tuple[str, str], ...],
    label: str,
) -> type:
    defining_class = _required_class(
        modules[defining_module], class_name, defining_module
    )
    for alias_module, alias_name in aliases:
        alias = _required_member(modules[alias_module], alias_name, alias_module)
        if alias is not defining_class:
            raise HermeticBootstrapError(
                f"pinned {label} alias {alias_module}.{alias_name} no longer "
                f"references {defining_module}.{class_name}"
            )
    return defining_class


def _require_transport_registry(
    owner: Any,
    owner_label: str,
    expected: dict[str, type],
) -> None:
    registry = _required_member(owner, "_transport_registry", owner_label)
    if not isinstance(registry, dict) or set(registry) != set(expected):
        raise HermeticBootstrapError(
            f"pinned transport registry {owner_label} has unexpected labels"
        )
    for label, defining_class in expected.items():
        if registry[label] is not defining_class:
            raise HermeticBootstrapError(
                f"pinned transport registry {owner_label}[{label!r}] no longer "
                "references its defining transport class"
            )


def _install_grpc_guards() -> None:
    channel_specs = (
        ("grpc", ("secure_channel", "insecure_channel")),
        ("grpc.aio", ("secure_channel", "insecure_channel")),
        ("grpc.experimental.aio", ("secure_channel", "insecure_channel")),
        ("grpc.aio._channel", ("secure_channel", "insecure_channel")),
        ("grpc.beta.implementations", ("secure_channel", "insecure_channel")),
        ("grpc._simple_stubs", ("_create_channel",)),
        ("google.api_core.grpc_helpers", ("create_channel",)),
        ("google.api_core.grpc_helpers_async", ("create_channel",)),
        ("google.auth.transport.grpc", ("secure_authorized_channel",)),
        (
            "google.cloud._helpers",
            ("make_secure_channel", "make_insecure_stub"),
        ),
    )
    channel_modules = {
        module_name: _required_module(module_name)
        for module_name, _ in channel_specs
    }
    for name in ("secure_channel", "insecure_channel"):
        sync_factory = _required_member(channel_modules["grpc"], name, "grpc")
        if (
            not callable(sync_factory)
            or getattr(sync_factory, "__module__", None) != "grpc"
            or getattr(sync_factory, "__qualname__", None) != name
        ):
            raise HermeticBootstrapError(
                f"grpcio {PINNED_GRPCIO_VERSION} defining factory grpc.{name} "
                "no longer has its audited descriptor"
            )
        for alias_name in ("grpc.aio", "grpc.experimental.aio"):
            _require_same_alias(
                channel_modules[alias_name],
                channel_modules["grpc.aio._channel"],
                name,
                alias_name,
                "grpc.aio._channel",
            )
    _patch_module_functions(
        channel_modules,
        channel_specs,
        "gRPC channel construction",
    )

    generated_services = (
        (
            "google.cloud.firestore_v1.services.firestore",
            "Firestore",
        ),
        (
            "google.cloud.firestore_admin_v1.services.firestore_admin",
            "FirestoreAdmin",
        ),
        ("google.cloud.tasks_v2.services.cloud_tasks", "CloudTasks"),
        ("google.cloud.tasks_v2beta2.services.cloud_tasks", "CloudTasks"),
        ("google.cloud.tasks_v2beta3.services.cloud_tasks", "CloudTasks"),
        ("google.cloud._storage_v2.services.storage", "Storage"),
    )
    generated_transports = ()
    rest_transports = ()
    for service, prefix in generated_services:
        transports = f"{service}.transports"
        grpc_module = f"{transports}.grpc"
        grpc_asyncio_module = f"{transports}.grpc_asyncio"
        client_module = f"{service}.client"
        async_client_module = f"{service}.async_client"
        sync_name = f"{prefix}GrpcTransport"
        async_name = f"{prefix}GrpcAsyncIOTransport"
        generated_transports += (
            (
                grpc_module,
                sync_name,
                (
                    (transports, sync_name),
                    (grpc_module, sync_name),
                    (grpc_asyncio_module, sync_name),
                    (client_module, sync_name),
                ),
            ),
            (
                grpc_asyncio_module,
                async_name,
                (
                    (transports, async_name),
                    (grpc_asyncio_module, async_name),
                    (client_module, async_name),
                    (async_client_module, async_name),
                ),
            ),
        )
        if prefix != "Storage":
            rest_module = f"{transports}.rest"
            rest_name = f"{prefix}RestTransport"
            rest_transports += (
                (
                    rest_module,
                    rest_name,
                    (
                        (transports, rest_name),
                        (rest_module, rest_name),
                        (client_module, rest_name),
                    ),
                ),
            )

    storage_client_aliases = (
        (
            "google.cloud.storage.grpc_client",
            "GrpcClient",
            (
                ("google.cloud.storage.grpc_client", "GrpcClient"),
                ("google.cloud.storage._experimental.grpc_client", "GrpcClient"),
            ),
        ),
        (
            "google.cloud.storage.asyncio.async_grpc_client",
            "AsyncGrpcClient",
            (
                (
                    "google.cloud.storage.asyncio.async_grpc_client",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage._experimental.asyncio.async_grpc_client",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage.asyncio.async_appendable_object_writer",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage.asyncio.async_multi_range_downloader",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage.asyncio.async_read_object_stream",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage.asyncio.async_write_object_stream",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage._experimental.asyncio."
                    "async_appendable_object_writer",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage._experimental.asyncio."
                    "async_multi_range_downloader",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage._experimental.asyncio."
                    "async_read_object_stream",
                    "AsyncGrpcClient",
                ),
                (
                    "google.cloud.storage._experimental.asyncio."
                    "async_write_object_stream",
                    "AsyncGrpcClient",
                ),
            ),
        ),
    )
    all_transport_classes = (
        generated_transports + rest_transports + storage_client_aliases
    )
    module_names = {
        name
        for defining_module, _, aliases in all_transport_classes
        for name in (defining_module, *(module for module, _ in aliases))
    }
    module_names.update(
        (
            "google.cloud.storage._http",
            "google.cloud.storage.batch",
            "google.cloud.storage.client",
        )
    )
    transport_modules = {
        name: _required_module(name) for name in sorted(module_names)
    }
    for defining_module, class_name, aliases in all_transport_classes:
        _require_class_aliases(
            transport_modules,
            defining_module,
            class_name,
            aliases,
            "Cloud transport",
        )
    for service, prefix in generated_services:
        transports = f"{service}.transports"
        client_module = f"{service}.client"
        expected_registry = {
            "grpc": _required_class(
                transport_modules[f"{transports}.grpc"],
                f"{prefix}GrpcTransport",
                f"{transports}.grpc",
            ),
            "grpc_asyncio": _required_class(
                transport_modules[f"{transports}.grpc_asyncio"],
                f"{prefix}GrpcAsyncIOTransport",
                f"{transports}.grpc_asyncio",
            ),
        }
        if prefix != "Storage":
            expected_registry["rest"] = _required_class(
                transport_modules[f"{transports}.rest"],
                f"{prefix}RestTransport",
                f"{transports}.rest",
            )
        _require_transport_registry(
            transport_modules[transports],
            transports,
            expected_registry,
        )
        client_class = _required_class(
            transport_modules[client_module],
            f"{prefix}Client",
            client_module,
        )
        _require_transport_registry(
            client_class,
            f"{client_module}.{prefix}Client",
            expected_registry,
        )
    for alias_module in (
        "google.cloud.storage.client",
        "google.cloud.storage.batch",
    ):
        _require_same_alias(
            transport_modules[alias_module],
            transport_modules["google.cloud.storage._http"],
            "Connection",
            alias_module,
            "google.cloud.storage._http",
        )

    constructor_specs = tuple(
        (defining_module, class_name, ("__init__",))
        for defining_module, class_name, _ in all_transport_classes
    ) + (("google.cloud.storage._http", "Connection", ("__init__",)),)
    _patch_class_members(
        transport_modules,
        constructor_specs,
        "Cloud client or transport construction",
        "method",
    )
    create_channel_specs = tuple(
        (defining_module, class_name, ("create_channel",))
        for defining_module, class_name, _ in generated_transports
    )
    _patch_class_members(
        transport_modules,
        create_channel_specs,
        "generated Cloud gRPC channel construction",
        "classmethod",
    )

    native_modules = {
        name: _required_module(name)
        for name in ("grpc._channel", "grpc.aio._channel", "grpc._cython.cygrpc")
    }
    for module_name, names in (
        ("grpc._channel", ("Channel",)),
        ("grpc.aio._channel", ("Channel",)),
        ("grpc._cython.cygrpc", ("Channel", "AioChannel")),
    ):
        module = native_modules[module_name]
        for name in names:
            original = _required_member(module, name, module_name)
            if not isinstance(original, type):
                raise HermeticBootstrapError(
                    f"grpcio {PINNED_GRPCIO_VERSION} native seam "
                    f"{module_name}.{name} is no longer a type"
                )
            setattr(module, name, BlockedGrpcChannel)


def _install_operations_guards() -> None:
    classes = (
        (
            "google.api_core.operations_v1.transports.rest",
            "OperationsRestTransport",
            (
                ("google.api_core.operations_v1", "OperationsRestTransport"),
                (
                    "google.api_core.operations_v1.transports",
                    "OperationsRestTransport",
                ),
                (
                    "google.api_core.operations_v1.transports.rest",
                    "OperationsRestTransport",
                ),
                (
                    "google.api_core.operations_v1.abstract_operations_base_client",
                    "OperationsRestTransport",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.transports.rest_asyncio",
            "AsyncOperationsRestTransport",
            (
                (
                    "google.api_core.operations_v1",
                    "AsyncOperationsRestTransport",
                ),
                (
                    "google.api_core.operations_v1.transports",
                    "AsyncOperationsRestTransport",
                ),
                (
                    "google.api_core.operations_v1.transports.rest_asyncio",
                    "AsyncOperationsRestTransport",
                ),
                (
                    "google.api_core.operations_v1.abstract_operations_base_client",
                    "AsyncOperationsRestTransport",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.operations_client",
            "OperationsClient",
            (
                ("google.api_core.operations_v1", "OperationsClient"),
                (
                    "google.api_core.operations_v1.operations_client",
                    "OperationsClient",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.operations_async_client",
            "OperationsAsyncClient",
            (
                ("google.api_core.operations_v1", "OperationsAsyncClient"),
                (
                    "google.api_core.operations_v1.operations_async_client",
                    "OperationsAsyncClient",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.abstract_operations_client",
            "AbstractOperationsClient",
            (
                ("google.api_core.operations_v1", "AbstractOperationsClient"),
                (
                    "google.api_core.operations_v1.abstract_operations_client",
                    "AbstractOperationsClient",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.operations_rest_client_async",
            "AsyncOperationsRestClient",
            (
                ("google.api_core.operations_v1", "AsyncOperationsRestClient"),
                (
                    "google.api_core.operations_v1.operations_rest_client_async",
                    "AsyncOperationsRestClient",
                ),
            ),
        ),
        (
            "google.api_core.operations_v1.abstract_operations_base_client",
            "AbstractOperationsBaseClient",
            (
                (
                    "google.api_core.operations_v1.abstract_operations_base_client",
                    "AbstractOperationsBaseClient",
                ),
                (
                    "google.api_core.operations_v1.abstract_operations_client",
                    "AbstractOperationsBaseClient",
                ),
                (
                    "google.api_core.operations_v1.operations_rest_client_async",
                    "AbstractOperationsBaseClient",
                ),
            ),
        ),
    )
    module_names = {
        name
        for defining_module, _, aliases in classes
        for name in (defining_module, *(module for module, _ in aliases))
    }
    modules = {name: _required_module(name) for name in sorted(module_names)}
    for defining_module, class_name, aliases in classes:
        _require_class_aliases(
            modules,
            defining_module,
            class_name,
            aliases,
            "Google Operations",
        )
    expected_registry = {
        "rest": _required_class(
            modules["google.api_core.operations_v1.transports.rest"],
            "OperationsRestTransport",
            "google.api_core.operations_v1.transports.rest",
        ),
        "rest_asyncio": _required_class(
            modules["google.api_core.operations_v1.transports.rest_asyncio"],
            "AsyncOperationsRestTransport",
            "google.api_core.operations_v1.transports.rest_asyncio",
        ),
    }
    registry_owners = (
        (
            modules["google.api_core.operations_v1.transports"],
            "google.api_core.operations_v1.transports",
        ),
        (
            _required_class(
                modules[
                    "google.api_core.operations_v1.abstract_operations_base_client"
                ],
                "AbstractOperationsBaseClient",
                "google.api_core.operations_v1.abstract_operations_base_client",
            ),
            "google.api_core.operations_v1.abstract_operations_base_client."
            "AbstractOperationsBaseClient",
        ),
        (
            _required_class(
                modules["google.api_core.operations_v1.abstract_operations_client"],
                "AbstractOperationsClient",
                "google.api_core.operations_v1.abstract_operations_client",
            ),
            "google.api_core.operations_v1.abstract_operations_client."
            "AbstractOperationsClient",
        ),
        (
            _required_class(
                modules[
                    "google.api_core.operations_v1.operations_rest_client_async"
                ],
                "AsyncOperationsRestClient",
                "google.api_core.operations_v1.operations_rest_client_async",
            ),
            "google.api_core.operations_v1.operations_rest_client_async."
            "AsyncOperationsRestClient",
        ),
    )
    for owner, owner_label in registry_owners:
        _require_transport_registry(owner, owner_label, expected_registry)

    for defining_module, class_name, _ in classes:
        _patch_class_members(
            modules,
            ((defining_module, class_name, ("__init__",)),),
            "Google Operations client or REST transport construction",
            "method",
        )


def _install_cloud_client_guards() -> None:
    client_classes = (
        (
            "google.cloud.firestore_v1.client",
            "Client",
            (
                ("google.cloud.firestore", "Client"),
                ("google.cloud.firestore_v1", "Client"),
                ("google.cloud.firestore_v1.client", "Client"),
            ),
        ),
        (
            "google.cloud.firestore_v1.async_client",
            "AsyncClient",
            (
                ("google.cloud.firestore", "AsyncClient"),
                ("google.cloud.firestore_v1", "AsyncClient"),
                ("google.cloud.firestore_v1.async_client", "AsyncClient"),
            ),
        ),
        (
            "google.cloud.firestore_v1.services.firestore.client",
            "FirestoreClient",
            (
                ("google.cloud.firestore_v1.services.firestore", "FirestoreClient"),
                (
                    "google.cloud.firestore_v1.services.firestore.client",
                    "FirestoreClient",
                ),
                (
                    "google.cloud.firestore_v1.services.firestore.async_client",
                    "FirestoreClient",
                ),
            ),
        ),
        (
            "google.cloud.firestore_v1.services.firestore.async_client",
            "FirestoreAsyncClient",
            (
                (
                    "google.cloud.firestore_v1.services.firestore",
                    "FirestoreAsyncClient",
                ),
                (
                    "google.cloud.firestore_v1.services.firestore.async_client",
                    "FirestoreAsyncClient",
                ),
            ),
        ),
        (
            "google.cloud.firestore_admin_v1.services.firestore_admin.client",
            "FirestoreAdminClient",
            (
                ("google.cloud.firestore_admin_v1", "FirestoreAdminClient"),
                ("google.cloud.firestore_admin", "FirestoreAdminClient"),
                (
                    "google.cloud.firestore_admin_v1.services.firestore_admin",
                    "FirestoreAdminClient",
                ),
                (
                    "google.cloud.firestore_admin_v1.services.firestore_admin.client",
                    "FirestoreAdminClient",
                ),
                (
                    "google.cloud.firestore_admin_v1.services.firestore_admin.async_client",
                    "FirestoreAdminClient",
                ),
            ),
        ),
        (
            "google.cloud.firestore_admin_v1.services.firestore_admin.async_client",
            "FirestoreAdminAsyncClient",
            (
                (
                    "google.cloud.firestore_admin_v1.services.firestore_admin",
                    "FirestoreAdminAsyncClient",
                ),
                (
                    "google.cloud.firestore_admin_v1.services.firestore_admin.async_client",
                    "FirestoreAdminAsyncClient",
                ),
            ),
        ),
        (
            "google.cloud.storage.client",
            "Client",
            (
                ("google.cloud.storage", "Client"),
                ("google.cloud.storage.client", "Client"),
                ("google.cloud.storage.transfer_manager", "Client"),
            ),
        ),
        (
            "google.cloud._storage_v2.services.storage.client",
            "StorageClient",
            (
                ("google.cloud._storage", "StorageClient"),
                ("google.cloud._storage_v2", "StorageClient"),
                ("google.cloud._storage_v2.services.storage", "StorageClient"),
                (
                    "google.cloud._storage_v2.services.storage.client",
                    "StorageClient",
                ),
                (
                    "google.cloud._storage_v2.services.storage.async_client",
                    "StorageClient",
                ),
            ),
        ),
        (
            "google.cloud._storage_v2.services.storage.async_client",
            "StorageAsyncClient",
            (
                ("google.cloud._storage", "StorageAsyncClient"),
                ("google.cloud._storage_v2", "StorageAsyncClient"),
                (
                    "google.cloud._storage_v2.services.storage",
                    "StorageAsyncClient",
                ),
                (
                    "google.cloud._storage_v2.services.storage.async_client",
                    "StorageAsyncClient",
                ),
            ),
        ),
    )
    for version in ("v2", "v2beta2", "v2beta3"):
        service = f"google.cloud.tasks_{version}.services.cloud_tasks"
        sync_versionless_aliases = (
            (("google.cloud.tasks", "CloudTasksClient"),)
            if version == "v2"
            else ()
        )
        async_versionless_aliases = (
            (("google.cloud.tasks", "CloudTasksAsyncClient"),)
            if version == "v2"
            else ()
        )
        client_classes += (
            (
                f"{service}.client",
                "CloudTasksClient",
                sync_versionless_aliases
                + (
                    (f"google.cloud.tasks_{version}", "CloudTasksClient"),
                    (service, "CloudTasksClient"),
                    (f"{service}.client", "CloudTasksClient"),
                    (f"{service}.async_client", "CloudTasksClient"),
                ),
            ),
            (
                f"{service}.async_client",
                "CloudTasksAsyncClient",
                async_versionless_aliases
                + (
                    (f"google.cloud.tasks_{version}", "CloudTasksAsyncClient"),
                    (service, "CloudTasksAsyncClient"),
                    (f"{service}.async_client", "CloudTasksAsyncClient"),
                ),
            ),
        )

    module_names = {
        module_name
        for defining_module, _, aliases in client_classes
        for module_name in (defining_module, *(name for name, _ in aliases))
    }
    modules = {name: _required_module(name) for name in sorted(module_names)}
    for defining_module, class_name, aliases in client_classes:
        defining_class = _required_class(
            modules[defining_module], class_name, defining_module
        )
        for alias_module, alias_name in aliases:
            alias = _required_member(modules[alias_module], alias_name, alias_module)
            if alias is not defining_class:
                raise HermeticBootstrapError(
                    f"pinned Cloud client alias {alias_module}.{alias_name} no "
                    f"longer references {defining_module}.{class_name}"
                )
        if "firestore" in defining_module:
            boundary = "Firestore client construction"
        elif "storage" in defining_module:
            boundary = "Cloud Storage client construction"
        else:
            boundary = "Cloud Tasks client construction"
        _patch_class_members(
            modules,
            ((defining_module, class_name, ("__init__",)),),
            boundary,
            "method",
        )

    genai_alias_modules = (
        "google.genai._api_client",
        "google.genai._live_converters",
        "google.genai._replay_api_client",
        "google.genai._tokens_converters",
        "google.genai.batches",
        "google.genai.caches",
        "google.genai.client",
        "google.genai.file_search_stores",
        "google.genai.live",
        "google.genai.live_music",
        "google.genai.models",
    )
    genai_modules = {
        name: _required_module(name) for name in genai_alias_modules
    }
    defining_genai_client = _required_class(
        genai_modules["google.genai._api_client"],
        "BaseApiClient",
        "google.genai._api_client",
    )
    for module_name in genai_alias_modules:
        alias = _required_member(
            genai_modules[module_name], "BaseApiClient", module_name
        )
        if alias is not defining_genai_client:
            raise HermeticBootstrapError(
                f"pinned GenAI alias {module_name}.BaseApiClient no longer "
                "references google.genai._api_client.BaseApiClient"
            )
    _patch_class_members(
        genai_modules,
        (("google.genai._api_client", "BaseApiClient", ("__init__",)),),
        "GenAI base client construction",
        "method",
    )

    gaos_module_names = (
        "google.genai._gaos",
        "google.genai._gaos.sdk",
        "google.genai._gaos.google_genai",
    )
    gaos_modules = {
        name: _required_module(name) for name in gaos_module_names
    }
    for class_name in ("GenAI", "AsyncGenAI"):
        aliases = tuple((name, class_name) for name in gaos_module_names)
        _require_class_aliases(
            gaos_modules,
            "google.genai._gaos.sdk",
            class_name,
            aliases,
            "GenAI GAOS",
        )
        _patch_class_members(
            gaos_modules,
            (("google.genai._gaos.sdk", class_name, ("__init__",)),),
            "GenAI GAOS client construction",
            "method",
        )

    for module_name, names in (
        ("google.genai", ("Client",)),
        ("google.genai.client", ("Client", "AsyncClient")),
    ):
        module = _required_module(module_name)
        for name in names:
            _required_member(module, name, module_name)
            setattr(module, name, BlockedGenAIClient)


def _violations_in(exc: BaseException):
    if isinstance(exc, HermeticTestViolation):
        yield exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _violations_in(nested)
    cause = exc.__cause__
    if isinstance(cause, BaseException):
        yield from _violations_in(cause)
    context = exc.__context__
    if isinstance(context, BaseException) and context is not cause:
        yield from _violations_in(context)


def _record_violation(exc: HermeticTestViolation) -> None:
    with _background_lock:
        _unacknowledged[id(exc)] = exc


def _record_exception_tree(exc: BaseException) -> None:
    for violation in _violations_in(exc):
        _record_violation(violation)


def acknowledge_violation(exc: BaseException) -> None:
    """Mark a deliberately asserted guard probe as expected."""
    with _background_lock:
        for violation in _violations_in(exc):
            _unacknowledged.pop(id(violation), None)


class _BackgroundBoundaryFailure(unittest.TestCase):
    def runTest(self) -> None:  # pragma: no cover - only names a result error
        pass

    def __str__(self) -> str:
        return "unhandled hermetic boundary violation in background work"


def _drain_background_failures():
    with _background_lock:
        failures = list(_unacknowledged.values())
        _unacknowledged.clear()
    return [(type(exc), exc, exc.__traceback__) for exc in failures]


def _install_background_failure_latch() -> None:
    original_loop_handler = asyncio.BaseEventLoop.call_exception_handler

    def loop_handler(loop: asyncio.BaseEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            _record_exception_tree(exc)
        original_loop_handler(loop, context)

    asyncio.BaseEventLoop.call_exception_handler = loop_handler

    original_thread_hook = threading.excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if isinstance(args.exc_value, BaseException):
            _record_exception_tree(args.exc_value)
        original_thread_hook(args)

    threading.excepthook = thread_hook

    original_unraisable_hook = sys.unraisablehook

    def unraisable_hook(args: SimpleNamespace) -> None:
        if isinstance(args.exc_value, BaseException):
            _record_exception_tree(args.exc_value)
        original_unraisable_hook(args)

    sys.unraisablehook = unraisable_hook

    original_add_error = unittest.TestResult.addError

    def add_error(
        result: unittest.TestResult,
        test: unittest.TestCase,
        info: tuple[type[BaseException], BaseException, Any],
    ) -> None:
        acknowledge_violation(info[1])
        original_add_error(result, test, info)

    unittest.TestResult.addError = add_error

    original_stop = unittest.TestResult.stopTestRun

    def stop_test_run(result: unittest.TestResult) -> None:
        for info in _drain_background_failures():
            result.addError(_BackgroundBoundaryFailure("runTest"), info)
        original_stop(result)

    unittest.TestResult.stopTestRun = stop_test_run

    def fail_at_process_exit() -> None:
        failures = _drain_background_failures()
        if not failures:
            return
        stream = sys.__stderr__ or sys.stderr
        stream.write("\nHermetic unittest boundary failed after the runner stopped.\n")
        for info in failures:
            traceback.print_exception(*info, file=stream)
        stream.flush()
        os._exit(86)

    atexit.register(fail_at_process_exit)


def install(source: str = "tests package fallback") -> None:
    """Install every guard, or reject a bootstrap that arrived too late."""
    global _installed, _install_source
    if _installed:
        return
    _assert_pinned_dependencies()
    _assert_clients_were_not_constructed_first()
    _assert_external_sdk_was_not_imported_first()
    _neutralize_external_configuration()
    _install_network_guards()
    _install_credential_guards()
    _install_grpc_guards()
    _install_operations_guards()
    _install_cloud_client_guards()
    _install_background_failure_latch()
    _install_source = source
    _installed = True

"""Gate 0A: audited fail-fast boundaries for supported unittest processes."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import google.auth
import google.auth._default as auth_default
from google import genai
from google.cloud import firestore_v1
from google.cloud import storage as google_storage
from google.cloud import tasks_v2
from google.cloud.firestore_v1 import async_client as firestore_async_client
from google.cloud.firestore_v1 import client as firestore_client
from google.cloud.firestore_v1.services import firestore as firestore_service
from google.cloud.firestore_v1.services.firestore import (
    async_client as firestore_service_async_client,
)
from google.cloud.firestore_v1.services.firestore import (
    client as firestore_service_client,
)
from google.cloud.storage import client as storage_client
from google.cloud.tasks_v2.services import cloud_tasks
from google.cloud.tasks_v2.services.cloud_tasks import (
    async_client as tasks_async_client,
)
from google.cloud.tasks_v2.services.cloud_tasks import client as tasks_client
from google.genai import client as genai_client

from core import coordinator, media, policy, settings, store
from core.models import Doctor, Loop, Patient
from sanad_test_guard import (
    HermeticTestViolation,
    acknowledge_violation,
    is_blocked_genai_client,
)


APP_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def _child_env(
    *, explicit: bool | None, extra: dict[str, str | None] | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    old_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(APP_ROOT) + (os.pathsep + old_path if old_path else "")
    if explicit is True:
        env["SANAD_TEST_MODE"] = "1"
    elif explicit is False:
        env["SANAD_TEST_MODE"] = "0"
    else:
        env.pop("SANAD_TEST_MODE", None)
    env["GOOGLE_CLOUD_PROJECT"] = "real-project-must-not-survive"
    env["GCLOUD_PROJECT"] = "real-project-must-not-survive"
    env["CLOUDSDK_CORE_PROJECT"] = "real-project-must-not-survive"
    env["GOOGLE_APPLICATION_CREDENTIALS"] = "/real/credentials/must-not-load.json"
    env["CLOUDSDK_CONFIG"] = "/real/gcloud/config/must-not-load"
    for name, value in (extra or {}).items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _child(
    *args: str,
    explicit: bool | None,
    timeout: float = 20,
    extra_env: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=APP_ROOT,
        env=_child_env(explicit=explicit, extra=extra_env),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _output(run: subprocess.CompletedProcess[str]) -> str:
    return f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"


class ExternalWorkFailsBeforeItCanLeaveTheProcess(unittest.TestCase):
    def assert_blocked(self, callable_, boundary: str) -> None:
        with self.assertRaises(HermeticTestViolation) as caught:
            callable_()
        self.assertIn(boundary, str(caught.exception))
        acknowledge_violation(caught.exception)

    def test_firestore_reexports_and_defining_modules_are_blocked(self) -> None:
        with patch.object(store, "_db", None):
            self.assert_blocked(store.db, "Firestore")
        for constructor in (
            firestore_v1.Client,
            firestore_v1.AsyncClient,
            firestore_client.Client,
            firestore_async_client.AsyncClient,
            firestore_service.FirestoreClient,
            firestore_service.FirestoreAsyncClient,
            firestore_service_client.FirestoreClient,
            firestore_service_async_client.FirestoreAsyncClient,
        ):
            self.assert_blocked(constructor, "Firestore")

    def test_lazy_cloud_client_definitions_and_reexports_are_blocked(self) -> None:
        for constructor, boundary in (
            (google_storage.Client, "Cloud Storage"),
            (storage_client.Client, "Cloud Storage"),
            (tasks_v2.CloudTasksClient, "Cloud Tasks"),
            (tasks_v2.CloudTasksAsyncClient, "Cloud Tasks"),
            (cloud_tasks.CloudTasksClient, "Cloud Tasks"),
            (cloud_tasks.CloudTasksAsyncClient, "Cloud Tasks"),
            (tasks_client.CloudTasksClient, "Cloud Tasks"),
            (tasks_async_client.CloudTasksAsyncClient, "Cloud Tasks"),
        ):
            self.assert_blocked(constructor, boundary)

    def test_every_google_credential_loader_is_blocked(self) -> None:
        for loader in (
            google.auth.default,
            google.auth.load_credentials_from_file,
            google.auth.load_credentials_from_dict,
            auth_default.default,
            auth_default.load_credentials_from_file,
            auth_default.load_credentials_from_dict,
            auth_default.get_api_key_credentials,
            auth_default._get_authorized_user_credentials,
            auth_default._get_external_account_authorized_user_credentials,
            auth_default._get_external_account_credentials,
            auth_default._get_explicit_environ_credentials,
            auth_default._get_gae_credentials,
            auth_default._get_gce_credentials,
            auth_default._get_gcloud_sdk_credentials,
            auth_default._get_gdch_service_account_credentials,
            auth_default._get_impersonated_service_account_credentials,
            auth_default._get_service_account_credentials,
            auth_default._load_credentials_from_info,
        ):
            self.assert_blocked(loader, "credential")

    def test_direct_google_credential_factories_are_blocked(self) -> None:
        async_default = importlib.import_module("google.auth._default_async")
        service_info = importlib.import_module("google.auth._service_account_info")
        id_token = importlib.import_module("google.oauth2.id_token")
        id_token_async = importlib.import_module("google.oauth2._id_token_async")

        module_calls = [
            (async_default.load_credentials_from_file, ("/never/credentials.json",)),
            (async_default.default_async, ()),
            (service_info.from_dict, ({},)),
            (service_info.from_filename, ("/never/credentials.json",)),
            (id_token.fetch_id_token_credentials, ("https://local.invalid",)),
            (id_token.fetch_id_token, (None, "https://local.invalid")),
            (id_token_async.fetch_id_token, (None, "https://local.invalid")),
        ]
        for optional_name, args in (
            ("default", ()),
            ("load_credentials_from_dict", ({},)),
            ("_get_explicit_environ_credentials", ()),
            ("_get_gae_credentials", ()),
            ("_get_gce_credentials", ()),
            ("_get_gcloud_sdk_credentials", ()),
        ):
            if hasattr(async_default, optional_name):
                module_calls.append((getattr(async_default, optional_name), args))

        for loader, args in module_calls:
            if inspect.iscoroutinefunction(loader):
                call = lambda loader=loader, args=args: asyncio.run(loader(*args))
            else:
                call = lambda loader=loader, args=args: loader(*args)
            self.assert_blocked(call, "credential")

        class_specs = (
            (
                "google.oauth2.service_account",
                ("Credentials", "IDTokenCredentials"),
                ("from_service_account_file", "from_service_account_info"),
            ),
            (
                "google.oauth2._service_account_async",
                ("Credentials", "IDTokenCredentials"),
                ("from_service_account_file", "from_service_account_info"),
            ),
            (
                "google.oauth2.credentials",
                ("Credentials",),
                ("from_authorized_user_file", "from_authorized_user_info"),
            ),
            (
                "google.oauth2._credentials_async",
                ("Credentials",),
                ("from_authorized_user_file", "from_authorized_user_info"),
            ),
            (
                "google.auth.jwt",
                ("Credentials", "OnDemandCredentials"),
                ("from_service_account_file", "from_service_account_info"),
            ),
            (
                "google.auth._jwt_async",
                ("Credentials", "OnDemandCredentials"),
                ("from_service_account_file", "from_service_account_info"),
            ),
            (
                "google.auth.identity_pool",
                ("Credentials",),
                ("from_file", "from_info"),
            ),
            (
                "google.auth.external_account",
                ("Credentials",),
                ("from_file", "from_info"),
            ),
            (
                "google.auth.external_account_authorized_user",
                ("Credentials",),
                ("from_file", "from_info"),
            ),
            (
                "google.auth.aws",
                ("Credentials",),
                ("from_file", "from_info"),
            ),
            (
                "google.auth.pluggable",
                ("Credentials",),
                ("from_file", "from_info"),
            ),
            (
                "google.oauth2.gdch_credentials",
                ("ServiceAccountCredentials",),
                ("from_service_account_file", "from_service_account_info"),
            ),
            (
                "google.auth.impersonated_credentials",
                ("Credentials",),
                ("from_impersonated_service_account_info",),
            ),
            (
                "google.auth.crypt.base",
                ("FromServiceAccountMixin",),
                ("from_service_account_file", "from_service_account_info"),
            ),
        )
        for module_name, class_names, method_names in class_specs:
            module = importlib.import_module(module_name)
            for class_name in class_names:
                credential_class = getattr(module, class_name)
                for method_name in method_names:
                    descriptor = inspect.getattr_static(credential_class, method_name)
                    self.assertIsInstance(descriptor, classmethod)
                    loader = getattr(credential_class, method_name)
                    arg = (
                        "/never/credentials.json"
                        if method_name.endswith("file")
                        else {}
                    )
                    self.assert_blocked(
                        lambda loader=loader, arg=arg: loader(arg), "credential"
                    )

        from google.auth import crypt

        for signer_class in (crypt.RSASigner, crypt.ES256Signer):
            for method_name in (
                "from_service_account_file",
                "from_service_account_info",
            ):
                loader = getattr(signer_class, method_name)
                arg = (
                    "/never/credentials.json"
                    if method_name.endswith("file")
                    else {}
                )
                self.assert_blocked(
                    lambda loader=loader, arg=arg: loader(arg), "credential"
                )

    def test_optional_python_rsa_backend_is_absent_or_guarded(self) -> None:
        if importlib.util.find_spec("rsa") is None:
            self.assertNotIn("google.auth.crypt._python_rsa", sys.modules)
            return
        python_rsa = importlib.import_module("google.auth.crypt._python_rsa")
        descriptor = inspect.getattr_static(python_rsa.RSASigner, "from_string")
        self.assertIsInstance(descriptor, classmethod)
        self.assert_blocked(
            lambda: python_rsa.RSASigner.from_string(b"secret-private-key"),
            "private-key",
        )

    def test_dns_tcp_and_udp_entry_points_are_blocked(self) -> None:
        calls = (
            lambda: socket.create_connection(("127.0.0.1", 9)),
            lambda: socket.getaddrinfo("127.0.0.1", 443),
            lambda: socket.getfqdn("127.0.0.1"),
            lambda: socket.gethostbyaddr("127.0.0.1"),
            lambda: socket.gethostbyname("localhost"),
            lambda: socket.gethostbyname_ex("localhost"),
            lambda: socket.getnameinfo(("127.0.0.1", 443), 0),
        )
        for call in calls:
            self.assert_blocked(call, "network")

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.assert_blocked(
                lambda: udp.sendto(b"never", ("127.0.0.1", 9)), "network"
            )
            if hasattr(udp, "sendmsg"):
                self.assert_blocked(
                    lambda: udp.sendmsg([b"never"], [], 0, ("127.0.0.1", 9)),
                    "network",
                )
        finally:
            udp.close()

    def test_genai_defining_constructor_and_every_unknown_property_are_blocked(
        self,
    ) -> None:
        for constructor in (genai.Client, genai_client.Client, genai_client.AsyncClient):
            self.assertTrue(is_blocked_genai_client(constructor()))
        for getter in (
            lambda: media.client.vertexai,
            lambda: media.client._api_client,
            lambda: media.client.aio.vertexai,
            lambda: media.client.aio.models.generate_content_stream,
        ):
            self.assert_blocked(getter, "GenAI")


class ApplicationFallbacksCannotHideTheBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_settings_fallback_cannot_hide_an_unmocked_firestore_call(
        self,
    ) -> None:
        with patch.object(store, "_db", None):
            with self.assertRaises(HermeticTestViolation) as caught:
                await settings.current()
        self.assertIn("Firestore", str(caught.exception))
        acknowledge_violation(caught.exception)

    async def test_the_existing_async_model_mock_seam_is_preserved(self) -> None:
        with self.assertRaises(HermeticTestViolation) as caught:
            await media.client.aio.models.generate_content(model="never")
        acknowledge_violation(caught.exception)

        async def local_double(**_kwargs):
            return "local"

        with patch.object(
            media.client.aio.models, "generate_content", local_double
        ):
            self.assertEqual(
                await media.client.aio.models.generate_content(model="fake"),
                "local",
            )

    async def test_real_adk_backend_inspection_escapes_the_coordinator_fallback(
        self,
    ) -> None:
        doctor = Doctor(id="d", name="Dr Test", web_token="t", created_at=NOW)
        patient = Patient(id="p", doctor_id="d", name="Test", created_at=NOW)
        loop = Loop(
            id="l",
            doctor_id="d",
            patient_id="p",
            type="TEST",
            title="Lipid panel",
            test_name="lipid panel",
            state="waiting_patient",
            due_at=NOW + timedelta(days=1),
            created_at=NOW,
            updated_at=NOW,
        )
        turn = coordinator.Turn(
            doctor=doctor,
            patient=patient,
            loop=loop,
            trigger=coordinator.WAKE,
            facts=policy.LoopFacts(now=NOW, due_at=loop.due_at),
            policy=policy.DEFAULT,
        )
        with patch.object(
            coordinator.events, "last_events", AsyncMock(return_value=[])
        ):
            with self.assertRaises(HermeticTestViolation) as caught:
                await asyncio.wait_for(coordinator.choose(turn), timeout=3)
        self.assertIn("GenAI", str(caught.exception))
        self.assertFalse(turn.model_failed)
        acknowledge_violation(caught.exception)


class FreshProcessBootstrap(unittest.TestCase):
    def assert_ok(self, run: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(run.returncode, 0, _output(run))
        self.assertIn("OK", run.stderr + run.stdout)

    def test_documented_discovery_bootstraps_before_imports(self) -> None:
        self.assert_ok(
            _child(
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
                "-p",
                "bootstrap_probe.py",
                "-q",
                explicit=True,
            )
        )

    def test_common_discovery_without_top_level_bootstraps_before_imports(
        self,
    ) -> None:
        self.assert_ok(
            _child(
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "bootstrap_probe.py",
                "-q",
                explicit=None,
            )
        )

    def test_focused_unittest_bootstraps_before_imports(self) -> None:
        self.assert_ok(
            _child(
                "-m",
                "unittest",
                "-q",
                "tests.bootstrap_probe",
                explicit=None,
            )
        )

    def test_supported_direct_file_command_bootstraps_before_imports(self) -> None:
        self.assert_ok(
            _child("tests/direct_probe_test.py", "-q", explicit=None)
        )

    def test_direct_startup_fails_closed_when_the_sdk_pin_drifts(self) -> None:
        marker = "DIRECT-TEST-BODY-MUST-NOT-RUN"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct_dir = root / "tests"
            direct_dir.mkdir()
            direct = direct_dir / "generic_direct_test.py"
            direct.write_text(
                f'print("{marker}")\n',
                encoding="utf-8",
            )

            # Python imports the first sitecustomize on PYTHONPATH.  This shim
            # changes the installed-version answer before loading Sanad's real
            # bootstrap, reproducing SDK drift during actual direct-file
            # interpreter startup rather than calling install() by hand.
            shim = root / "sitecustomize.py"
            shim.write_text(
                textwrap.dedent(
                    """
                    import importlib.metadata
                    import importlib.util
                    import os
                    import sys

                    real_version = importlib.metadata.version

                    def forced_version(name):
                        if name == "google-auth":
                            return "2.58.0"
                        return real_version(name)

                    importlib.metadata.version = forced_version
                    spec = importlib.util.spec_from_file_location(
                        "_sanad_real_sitecustomize",
                        os.environ["SANAD_REAL_SITECUSTOMIZE"],
                    )
                    assert spec is not None and spec.loader is not None
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    """
                ),
                encoding="utf-8",
            )
            run = _child(
                str(direct),
                explicit=None,
                extra_env={
                    "PYTHONPATH": os.pathsep.join((str(root), str(APP_ROOT))),
                    "SANAD_REAL_SITECUSTOMIZE": str(APP_ROOT / "sitecustomize.py"),
                },
            )

        output = _output(run)
        self.assertNotEqual(run.returncode, 0, output)
        self.assertIn("Sanad hermetic unittest bootstrap failed", run.stderr)
        self.assertIn("HermeticBootstrapError", run.stderr)
        self.assertIn("found 2.58.0", run.stderr)
        self.assertNotIn(marker, run.stdout + run.stderr)

    def test_production_style_startup_leaves_the_guard_inert(self) -> None:
        code = textwrap.dedent(
            """
            import os
            import socket
            import sys
            before_environment = dict(os.environ)
            before_getaddrinfo = socket.getaddrinfo
            before_connect = socket.socket.connect
            assert "sanad_test_guard" not in sys.modules
            import main
            import tests
            assert "sanad_test_guard" not in sys.modules
            assert dict(os.environ) == before_environment
            assert socket.getaddrinfo is before_getaddrinfo
            assert socket.socket.connect is before_connect
            assert not getattr(main.media.client, "_sanad_hermetic", False)
            assert main.media.client.__class__.__module__ == "google.genai.client"
            print("production bootstrap inert")
            """
        )
        for explicit in (None, False):
            with self.subTest(explicit=explicit):
                run = _child("-c", code, "uvicorn", "main:app", explicit=explicit)
                self.assertEqual(run.returncode, 0, _output(run))
                self.assertIn("production bootstrap inert", run.stdout)

    def test_late_install_rejects_an_already_constructed_media_client(self) -> None:
        code = textwrap.dedent(
            """
            import os
            from google import genai
            from google.genai import client as genai_client
            class RealClientMarker:
                pass
            def make_real_marker(*args, **kwargs):
                return RealClientMarker()
            genai.Client = make_real_marker
            genai_client.Client = make_real_marker
            import core.media
            os.environ["SANAD_TEST_MODE"] = "1"
            import tests
            """
        )
        run = _child("-c", code, explicit=None)
        self.assertNotEqual(run.returncode, 0, _output(run))
        self.assertIn("late test bootstrap", run.stderr)
        self.assertIn("real GenAI client", run.stderr)

    def test_late_install_rejects_an_already_cached_firestore_client(self) -> None:
        code = textwrap.dedent(
            """
            import os
            import core.store
            core.store._db = object()
            os.environ["SANAD_TEST_MODE"] = "1"
            import tests
            """
        )
        run = _child("-c", code, explicit=None)
        self.assertNotEqual(run.returncode, 0, _output(run))
        self.assertIn("late test bootstrap", run.stderr)
        self.assertIn("real Firestore client", run.stderr)

    def test_cached_socket_aliases_are_still_stopped_by_the_audit_hook(self) -> None:
        code = textwrap.dedent(
            """
            import os
            import socket
            old_getaddrinfo = socket.getaddrinfo
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            old_sendto = udp.sendto
            old_sendmsg = getattr(udp, "sendmsg", None)
            udp.close()
            os.environ["SANAD_TEST_MODE"] = "1"
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation, install,
            )
            install("cached-alias probe")
            calls = [lambda: old_getaddrinfo("127.0.0.1", 443),
                     lambda: old_sendto(b"x", ("127.0.0.1", 9))]
            if old_sendmsg is not None:
                calls.append(
                    lambda: old_sendmsg([b"x"], [], 0, ("127.0.0.1", 9))
                )
            for call in calls:
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                else:
                    raise AssertionError("a cached socket alias escaped")
            print("cached aliases blocked")
            """
        )
        run = _child("-c", code, explicit=None)
        self.assertEqual(run.returncode, 0, _output(run))
        self.assertIn("cached aliases blocked", run.stdout)

    def test_detached_task_and_thread_violations_make_unittest_fail(self) -> None:
        code = textwrap.dedent(
            """
            import asyncio
            import threading
            import unittest
            from core import store
            kept = []
            class Leaks(unittest.IsolatedAsyncioTestCase):
                async def test_detached_task_cannot_hide_the_violation(self):
                    async def swallowed():
                        try:
                            store.db()
                        except BaseException:
                            pass
                    kept.append(asyncio.create_task(swallowed()))
                    await asyncio.sleep(0)
                async def test_thread_cannot_hide_the_violation(self):
                    def swallowed():
                        try:
                            store.db()
                        except BaseException:
                            pass
                    thread = threading.Thread(target=swallowed)
                    thread.start()
                    thread.join()
            unittest.main(verbosity=0)
            """
        )
        run = _child("-c", code, explicit=True)
        self.assertNotEqual(run.returncode, 0, _output(run))
        self.assertIn("unhandled hermetic boundary violation", run.stderr)
        self.assertIn("FAILED", run.stderr)


class FreshProcessAdversarialCanaries(unittest.TestCase):
    """Fixed attacks with independent side-effect spies, not guard-table mirrors."""

    def assert_ok(self, run: subprocess.CompletedProcess[str], marker: str) -> None:
        self.assertEqual(run.returncode, 0, _output(run))
        self.assertIn(marker, run.stdout)

    def test_bootstrap_neutralizes_secret_environment_before_google_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            certificate = root / "agent-cert.pem"
            certificate.write_text("BOOT-SECRET-CERTIFICATE", encoding="utf-8")
            config = root / "certificate-config.json"
            config.write_text(
                '{"cert_configs":{"workload":{"cert_path":"'
                + str(certificate)
                + '","key_path":"'
                + str(certificate)
                + '"}}}',
                encoding="utf-8",
            )
            code = textwrap.dedent(
                """
                import builtins
                import io
                import os
                import subprocess

                watched = {
                    os.environ["ROUND4_CERT_CONFIG"],
                    os.environ["ROUND4_CERT_FILE"],
                }
                side_effects = []
                real_open = builtins.open
                real_io_open = io.open
                def watch_open(real):
                    def guarded(file, *args, **kwargs):
                        try:
                            path = os.fspath(file)
                        except TypeError:
                            path = None
                        if path in watched:
                            side_effects.append(("open", path))
                            raise AssertionError("credential file opened during bootstrap")
                        return real(file, *args, **kwargs)
                    return guarded
                builtins.open = watch_open(real_open)
                io.open = watch_open(real_io_open)
                def no_subprocess(*args, **kwargs):
                    side_effects.append(("subprocess", args))
                    raise AssertionError("credential subprocess ran during bootstrap")
                subprocess.run = no_subprocess
                subprocess.Popen = no_subprocess
                subprocess.check_output = no_subprocess

                os.environ["SANAD_TEST_MODE"] = "1"
                from sanad_test_guard import install
                install("round4 boot canary")

                assert side_effects == [], side_effects
                for name in (
                    "GOOGLE_API_CERTIFICATE_CONFIG",
                    "CLOUDSDK_CONTEXT_AWARE_CERTIFICATE_CONFIG_FILE_PATH",
                    "AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY",
                    "AWS_SESSION_TOKEN",
                    "AWS_REGION",
                    "AWS_WEB_IDENTITY_TOKEN_FILE",
                    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
                ):
                    assert name not in os.environ, (name, os.environ.get(name))
                assert os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] == "false"
                assert os.environ["GOOGLE_API_USE_MTLS_ENDPOINT"] == "never"
                assert os.environ["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] == "0"
                assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
                assert os.environ["NO_GCE_CHECK"] == "true"
                print("boot-canaries=12")
                """
            )
            extra = {
                "ROUND4_CERT_CONFIG": str(config),
                "ROUND4_CERT_FILE": str(certificate),
                "GOOGLE_API_CERTIFICATE_CONFIG": str(config),
                "CLOUDSDK_CONTEXT_AWARE_CERTIFICATE_CONFIG_FILE_PATH": str(config),
                "GOOGLE_API_USE_CLIENT_CERTIFICATE": "true",
                "GOOGLE_API_USE_MTLS_ENDPOINT": "always",
                "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES": "1",
                "AWS_ACCESS_KEY_ID": "ROUND4_ACCESS_SECRET",
                "AWS_SECRET_ACCESS_KEY": "ROUND4_KEY_SECRET",
                "AWS_SESSION_TOKEN": "ROUND4_SESSION_SECRET",
                "AWS_REGION": "secret-region-1",
                "AWS_WEB_IDENTITY_TOKEN_FILE": str(certificate),
                "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE": str(certificate),
            }
            self.assert_ok(
                _child("-c", code, explicit=None, extra_env=extra),
                "boot-canaries=12",
            )

    def test_gcloud_external_account_and_plugin_acquisition_fail_before_io(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "subject-token.txt"
            secret.write_text("ROUND4-SUBJECT-TOKEN-SECRET", encoding="utf-8")
            code = textwrap.dedent(
                """
                import builtins
                import io
                import os
                import subprocess
                import warnings
                from sanad_test_guard import (
                    HermeticTestViolation, acknowledge_violation,
                )
                from google.auth import _cloud_sdk, aws, identity_pool, pluggable
                from google.oauth2 import credentials as oauth_credentials
                from google.oauth2 import webauthn_handler

                secret = os.environ["ROUND4_SECRET_FILE"]
                side_effects = []
                real_open = builtins.open
                real_io_open = io.open
                def watch_open(real):
                    def guarded(file, *args, **kwargs):
                        if os.fspath(file) == secret:
                            side_effects.append(("open", secret))
                            raise AssertionError("secret credential file opened")
                        return real(file, *args, **kwargs)
                    return guarded
                builtins.open = watch_open(real_open)
                io.open = watch_open(real_io_open)
                def trip(kind):
                    def reached(*args, **kwargs):
                        side_effects.append((kind, args, kwargs))
                        raise AssertionError("credential side effect reached: " + kind)
                    return reached
                subprocess.run = trip("subprocess.run")
                subprocess.Popen = trip("subprocess.Popen")
                subprocess.check_output = trip("subprocess.check_output")
                request = trip("request")
                count = 0
                def expect(call):
                    global count
                    try:
                        call()
                    except HermeticTestViolation as exc:
                        acknowledge_violation(exc)
                        count += 1
                    else:
                        raise AssertionError("credential acquisition was not blocked")

                expect(_cloud_sdk.get_project_id)
                expect(_cloud_sdk.get_auth_access_token)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    user = oauth_credentials.UserAccessTokenCredentials()
                expect(lambda: user.refresh(None))

                aws_cred = aws.Credentials(
                    audience="//iam.googleapis.com/projects/1/locations/global/"
                             "workloadIdentityPools/p/providers/aws",
                    subject_token_type="urn:ietf:params:aws:token-type:aws4_request",
                    credential_source={
                        "environment_id": "aws1",
                        "regional_cred_verification_url":
                            "https://sts.{region}.amazonaws.com?Action=GetCallerIdentity",
                        "region_url": "http://round4.invalid/region",
                        "url": "http://round4.invalid/credentials",
                        "imdsv2_session_token_url": "http://round4.invalid/token",
                    },
                )
                expect(lambda: aws_cred.retrieve_subject_token(request))

                identity = identity_pool.Credentials(
                    audience="//iam.googleapis.com/projects/1/locations/global/"
                             "workloadIdentityPools/p/providers/oidc",
                    subject_token_type="urn:ietf:params:oauth:token-type:jwt",
                    credential_source={"file": secret},
                )
                expect(lambda: identity.retrieve_subject_token(request))
                file_supplier = identity_pool._FileSupplier(secret, "text", None)
                expect(lambda: file_supplier.get_subject_token(None, request))
                url_supplier = identity_pool._UrlSupplier(
                    "http://round4.invalid/token", "text", None, {}
                )
                expect(lambda: url_supplier.get_subject_token(None, request))
                identity_url = identity_pool.Credentials(
                    audience="//iam.googleapis.com/projects/1/locations/global/"
                             "workloadIdentityPools/p/providers/url",
                    subject_token_type="urn:ietf:params:oauth:token-type:jwt",
                    credential_source={"url": "http://round4.invalid/token"},
                )
                expect(lambda: identity_url.retrieve_subject_token(request))
                identity_x509 = identity_pool.Credentials(
                    audience="//iam.googleapis.com/projects/1/locations/global/"
                             "workloadIdentityPools/p/providers/x509",
                    subject_token_type="urn:ietf:params:oauth:token-type:jwt",
                    credential_source={"certificate": {
                        "certificate_config_location": secret,
                        "trust_chain_path": secret,
                    }},
                )
                expect(lambda: identity_x509.retrieve_subject_token(request))
                x509_supplier = identity_pool._X509Supplier(
                    secret, trip("x509 certificate callback")
                )
                expect(lambda: x509_supplier.get_subject_token(None, request))

                executable = pluggable.Credentials(
                    audience="//iam.googleapis.com/locations/global/"
                             "workforcePools/p/providers/executable",
                    subject_token_type="urn:ietf:params:oauth:token-type:jwt",
                    token_url="https://sts.googleapis.com/v1/token",
                    credential_source={"executable": {
                        "command": "/round4/credential-provider",
                        "timeout_millis": 5000,
                        "output_file": secret,
                    }},
                )
                expect(lambda: executable.retrieve_subject_token(request))
                expect(lambda: executable.revoke(request))
                plugin = webauthn_handler.PluginHandler()
                expect(lambda: plugin.get(object()))

                assert side_effects == [], side_effects
                assert "AWS_ACCESS_KEY_ID" not in os.environ
                assert os.environ["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] == "0"
                print(f"external-acquisition-canaries={count}")
                """
            )
            extra = {
                "ROUND4_SECRET_FILE": str(secret),
                "AWS_ACCESS_KEY_ID": "ROUND4_ACCESS_SECRET",
                "AWS_SECRET_ACCESS_KEY": "ROUND4_KEY_SECRET",
                "AWS_SESSION_TOKEN": "ROUND4_SESSION_SECRET",
                "AWS_REGION": "secret-region-1",
                "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES": "1",
                "GOOGLE_AUTH_WEBAUTHN_PLUGIN": "/round4/webauthn-provider",
            }
            self.assert_ok(
                _child("-c", code, explicit=True, extra_env=extra),
                "external-acquisition-canaries=13",
            )

    def test_compute_residency_aliases_fail_before_open(self) -> None:
        code = textwrap.dedent(
            """
            import builtins
            import io
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )
            from google.auth import compute_engine
            from google.auth.compute_engine import _metadata

            opens = []
            def trip_open(*args, **kwargs):
                opens.append((args, kwargs))
                raise AssertionError("compute residency touched the filesystem")
            builtins.open = trip_open
            io.open = trip_open

            count = 0
            for call in (
                compute_engine.detect_gce_residency_linux,
                _metadata.detect_gce_residency_linux,
            ):
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("compute residency detection escaped")
            assert opens == [], opens
            print(f"compute-residency-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "compute-residency-canaries=2",
        )

    def test_private_signer_state_hooks_fail_before_serialization(self) -> None:
        code = textwrap.dedent(
            """
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )
            from google.auth import crypt
            from google.auth.crypt import _cryptography_rsa, es

            private_side_effects = []
            public_parses = []

            def private_parse(*args, **kwargs):
                private_side_effects.append(("load_pem_private_key", args, kwargs))
                raise AssertionError("private-key parser reached")
            _cryptography_rsa.serialization.load_pem_private_key = private_parse
            es.serialization.load_pem_private_key = private_parse

            class PrivateKey:
                def private_bytes(self, *args, **kwargs):
                    private_side_effects.append(("private_bytes", args, kwargs))
                    raise AssertionError("private-key serialization reached")

            rsa_get = object.__new__(_cryptography_rsa.RSASigner)
            rsa_get._key = PrivateKey()
            rsa_get._key_id = None
            rsa_set = object.__new__(_cryptography_rsa.RSASigner)
            ec_get = object.__new__(es.EsSigner)
            ec_get._key = PrivateKey()
            ec_get._key_id = None
            ec_set = object.__new__(es.EsSigner)

            count = 0
            for call in (
                rsa_get.__getstate__,
                lambda: rsa_set.__setstate__({"_key": b"secret-rsa"}),
                ec_get.__getstate__,
                lambda: ec_set.__setstate__({"_key": b"secret-ec"}),
            ):
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("private signer state hook escaped")

            public_key = object()
            def public_parse(data, backend):
                public_parses.append(data)
                return public_key
            _cryptography_rsa.serialization.load_pem_public_key = public_parse
            verifier = crypt.RSAVerifier.from_string(b"public-key")
            assert verifier is not None
            assert public_parses == [b"public-key"], public_parses
            assert private_side_effects == [], private_side_effects
            print(f"signer-state-canaries={count}; public-verifier-parses=1")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "signer-state-canaries=4; public-verifier-parses=1",
        )

    def test_grpc_aliases_helpers_and_native_backstops_fail_before_channels(
        self,
    ) -> None:
        code = textwrap.dedent(
            """
            import grpc
            import grpc._channel as sync_channel
            import grpc._simple_stubs as simple_stubs
            import grpc.aio as aio
            import grpc.aio._channel as aio_channel
            import grpc.beta.implementations as beta
            import grpc.experimental.aio as experimental_aio
            from grpc._cython import cygrpc
            from google.api_core import grpc_helpers, grpc_helpers_async
            from google.auth import credentials as auth_credentials
            from google.auth.transport import grpc as auth_grpc
            from google.cloud import _helpers as cloud_helpers
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )

            anonymous = auth_credentials.AnonymousCredentials()
            channel_credentials = grpc.local_channel_credentials()
            side_effects = []
            count = 0

            def expect(call):
                global count
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("gRPC route was not blocked")

            for call in (
                lambda: sync_channel.Channel("local", (), None, None),
                lambda: aio_channel.Channel("local", (), None, None, None),
                lambda: cygrpc.Channel(),
                lambda: cygrpc.AioChannel(),
            ):
                expect(call)

            def spy_type(kind):
                class Reached:
                    def __new__(cls, *args, **kwargs):
                        side_effects.append((kind, args, kwargs))
                        raise AssertionError("native gRPC channel reached: " + kind)
                return Reached

            sync_channel.Channel = spy_type("grpc._channel.Channel")
            aio_channel.Channel = spy_type("grpc.aio._channel.Channel")
            cygrpc.Channel = spy_type("cygrpc.Channel")
            cygrpc.AioChannel = spy_type("cygrpc.AioChannel")

            calls = (
                lambda: grpc.secure_channel("127.0.0.1:1", channel_credentials),
                lambda: grpc.insecure_channel("127.0.0.1:1"),
                lambda: aio.secure_channel("127.0.0.1:1", channel_credentials),
                lambda: aio.insecure_channel("127.0.0.1:1"),
                lambda: experimental_aio.secure_channel(
                    "127.0.0.1:1", channel_credentials
                ),
                lambda: experimental_aio.insecure_channel("127.0.0.1:1"),
                lambda: aio_channel.secure_channel(
                    "127.0.0.1:1", channel_credentials
                ),
                lambda: aio_channel.insecure_channel("127.0.0.1:1"),
                lambda: beta.secure_channel("127.0.0.1", 1, channel_credentials),
                lambda: beta.insecure_channel("127.0.0.1", 1),
                lambda: simple_stubs._create_channel(
                    "127.0.0.1:1", (), channel_credentials, None
                ),
                lambda: auth_grpc.secure_authorized_channel(
                    anonymous, object(), "127.0.0.1:1"
                ),
                lambda: cloud_helpers.make_secure_channel(
                    anonymous, "sanad-test", "127.0.0.1:1"
                ),
                lambda: cloud_helpers.make_insecure_stub(
                    object, "127.0.0.1", 1
                ),
            )
            for call in calls:
                expect(call)

            def trip_factory(kind):
                def reached(*args, **kwargs):
                    side_effects.append((kind, args, kwargs))
                    raise AssertionError("public gRPC factory reached: " + kind)
                return reached
            for owner in (grpc, aio, experimental_aio, aio_channel):
                owner.secure_channel = trip_factory("secure_channel")
                owner.insecure_channel = trip_factory("insecure_channel")
            expect(lambda: grpc_helpers.create_channel(
                "127.0.0.1:1", credentials=anonymous
            ))
            expect(lambda: grpc_helpers_async.create_channel(
                "127.0.0.1:1", credentials=anonymous
            ))

            assert side_effects == [], side_effects
            print(f"grpc-entrypoint-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "grpc-entrypoint-canaries=20",
        )

    def test_cloud_transport_and_client_constructors_fail_before_channels(
        self,
    ) -> None:
        code = textwrap.dedent(
            """
            import grpc
            import grpc._channel as sync_channel
            import grpc.aio as aio
            import grpc.aio._channel as aio_channel
            import grpc.experimental.aio as experimental_aio
            import httpx
            from grpc._cython import cygrpc
            from google.auth.credentials import AnonymousCredentials
            from google.auth.transport import requests as auth_requests
            from google.cloud import (
                _storage_v2 as storage_v2, firestore, storage, tasks_v2,
                tasks_v2beta2, tasks_v2beta3,
            )
            from google.cloud._storage_v2.services.storage import transports as storage_t
            from google.cloud._storage_v2.services.storage.transports import (
                base as storage_base,
            )
            from google.cloud.firestore_admin_v1.services import (
                firestore_admin as firestore_admin_service,
            )
            from google.cloud.firestore_admin_v1.services.firestore_admin import (
                transports as firestore_admin_t,
            )
            from google.cloud.firestore_admin_v1.services.firestore_admin.transports import (
                base as firestore_admin_base,
            )
            from google.cloud.firestore_admin_v1.services.firestore_admin.transports import (
                rest as firestore_admin_rest,
            )
            from google.cloud.firestore_v1.services import (
                firestore as firestore_service,
            )
            from google.cloud.firestore_v1.services.firestore import (
                transports as firestore_t,
            )
            from google.cloud.firestore_v1.services.firestore.transports import (
                base as firestore_base,
            )
            from google.cloud.firestore_v1.services.firestore.transports import (
                rest as firestore_rest,
            )
            from google.cloud.storage import _http as storage_http
            from google.cloud.storage import grpc_client as storage_grpc
            from google.cloud.storage.asyncio import (
                async_grpc_client as storage_async_grpc,
            )
            from google.cloud.tasks_v2.services.cloud_tasks import (
                transports as tasks_v2_t,
            )
            from google.cloud.tasks_v2.services.cloud_tasks.transports import (
                base as tasks_v2_base,
            )
            from google.cloud.tasks_v2.services.cloud_tasks.transports import (
                rest as tasks_v2_rest,
            )
            from google.cloud.tasks_v2beta2.services.cloud_tasks import (
                transports as tasks_v2beta2_t,
            )
            from google.cloud.tasks_v2beta2.services.cloud_tasks.transports import (
                base as tasks_v2beta2_base,
            )
            from google.cloud.tasks_v2beta2.services.cloud_tasks.transports import (
                rest as tasks_v2beta2_rest,
            )
            from google.cloud.tasks_v2beta3.services.cloud_tasks import (
                transports as tasks_v2beta3_t,
            )
            from google.cloud.tasks_v2beta3.services.cloud_tasks.transports import (
                base as tasks_v2beta3_base,
            )
            from google.cloud.tasks_v2beta3.services.cloud_tasks.transports import (
                rest as tasks_v2beta3_rest,
            )
            from google.genai import _api_client as genai_api_client
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )

            anonymous = AnonymousCredentials()
            side_effects = []
            count = 0

            def trip(kind):
                def reached(*args, **kwargs):
                    side_effects.append((kind, args, kwargs))
                    raise AssertionError("external side effect reached: " + kind)
                return reached

            def spy_type(kind):
                class Reached:
                    def __new__(cls, *args, **kwargs):
                        side_effects.append((kind, args, kwargs))
                        raise AssertionError("native channel reached: " + kind)
                return Reached

            for owner in (grpc, aio, experimental_aio, aio_channel):
                owner.secure_channel = trip("secure_channel")
                owner.insecure_channel = trip("insecure_channel")
            sync_channel.Channel = spy_type("grpc._channel.Channel")
            aio_channel.Channel = spy_type("grpc.aio._channel.Channel")
            cygrpc.Channel = spy_type("cygrpc.Channel")
            cygrpc.AioChannel = spy_type("cygrpc.AioChannel")
            auth_requests.AuthorizedSession.__init__ = trip("AuthorizedSession")
            for rest_module in (
                firestore_rest,
                firestore_admin_rest,
                tasks_v2_rest,
                tasks_v2beta2_rest,
                tasks_v2beta3_rest,
            ):
                rest_module.AuthorizedSession = trip(
                    rest_module.__name__ + ".AuthorizedSession"
                )
            httpx.Client = trip("httpx.Client")
            httpx.AsyncClient = trip("httpx.AsyncClient")

            def expect(call):
                global count
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("Cloud client or transport escaped")

            grpc_transports = (
                firestore_t.FirestoreGrpcTransport,
                firestore_t.FirestoreGrpcAsyncIOTransport,
                firestore_admin_t.FirestoreAdminGrpcTransport,
                firestore_admin_t.FirestoreAdminGrpcAsyncIOTransport,
                tasks_v2_t.CloudTasksGrpcTransport,
                tasks_v2_t.CloudTasksGrpcAsyncIOTransport,
                tasks_v2beta2_t.CloudTasksGrpcTransport,
                tasks_v2beta2_t.CloudTasksGrpcAsyncIOTransport,
                tasks_v2beta3_t.CloudTasksGrpcTransport,
                tasks_v2beta3_t.CloudTasksGrpcAsyncIOTransport,
                storage_t.StorageGrpcTransport,
                storage_t.StorageGrpcAsyncIOTransport,
            )
            for transport in grpc_transports:
                expect(lambda transport=transport: transport(credentials=anonymous))
                expect(
                    lambda transport=transport: transport.create_channel(
                        credentials=anonymous
                    )
                )

            for transport in (
                firestore_t.FirestoreRestTransport,
                firestore_admin_t.FirestoreAdminRestTransport,
                tasks_v2_t.CloudTasksRestTransport,
                tasks_v2beta2_t.CloudTasksRestTransport,
                tasks_v2beta3_t.CloudTasksRestTransport,
            ):
                expect(lambda transport=transport: transport(credentials=anonymous))

            for client in (
                storage_grpc.GrpcClient,
                storage_async_grpc.AsyncGrpcClient,
            ):
                expect(lambda client=client: client(credentials=anonymous))

            expect(lambda: storage_http.Connection(storage.Client, client_info=None))

            inert_firestore = firestore_base.FirestoreTransport(
                credentials=anonymous
            )
            inert_admin = firestore_admin_base.FirestoreAdminTransport(
                credentials=anonymous
            )
            inert_tasks_v2 = tasks_v2_base.CloudTasksTransport(
                credentials=anonymous
            )
            inert_tasks_v2beta2 = tasks_v2beta2_base.CloudTasksTransport(
                credentials=anonymous
            )
            inert_tasks_v2beta3 = tasks_v2beta3_base.CloudTasksTransport(
                credentials=anonymous
            )
            inert_storage = storage_base.StorageTransport(credentials=anonymous)

            for client, transport in (
                (firestore_service.FirestoreClient, inert_firestore),
                (firestore_service.FirestoreAsyncClient, inert_firestore),
                (firestore_admin_service.FirestoreAdminClient, inert_admin),
                (firestore_admin_service.FirestoreAdminAsyncClient, inert_admin),
                (tasks_v2.CloudTasksClient, inert_tasks_v2),
                (tasks_v2.CloudTasksAsyncClient, inert_tasks_v2),
                (tasks_v2beta2.CloudTasksClient, inert_tasks_v2beta2),
                (tasks_v2beta2.CloudTasksAsyncClient, inert_tasks_v2beta2),
                (tasks_v2beta3.CloudTasksClient, inert_tasks_v2beta3),
                (tasks_v2beta3.CloudTasksAsyncClient, inert_tasks_v2beta3),
                (storage_v2.StorageClient, inert_storage),
                (storage_v2.StorageAsyncClient, inert_storage),
            ):
                expect(
                    lambda client=client, transport=transport: client(
                        transport=transport
                    )
                )

            for client in (firestore.Client, firestore.AsyncClient, storage.Client):
                expect(
                    lambda client=client: client(
                        project="sanad-test", credentials=anonymous
                    )
                )

            expect(lambda: genai_api_client.BaseApiClient(api_key="test"))

            assert side_effects == [], side_effects
            print(f"cloud-constructor-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "cloud-constructor-canaries=48",
        )

    def test_operations_clients_fail_at_their_own_constructor_boundaries(
        self,
    ) -> None:
        code = textwrap.dedent(
            """
            from google.api_core import operations_v1
            from google.api_core.operations_v1 import (
                abstract_operations_base_client as operations_base_client,
            )
            from google.api_core.operations_v1.transports import (
                base as operations_transport_base,
                rest as operations_rest,
                rest_asyncio as operations_rest_asyncio,
            )
            from google.auth.credentials import AnonymousCredentials
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )

            anonymous = AnonymousCredentials()
            side_effects = []
            count = 0

            def trip(kind):
                def reached(*args, **kwargs):
                    side_effects.append((kind, args, kwargs))
                    raise AssertionError("Operations side effect reached: " + kind)
                return reached

            operations_rest.AuthorizedSession = trip("AuthorizedSession")
            operations_rest_asyncio.AsyncAuthorizedSession = trip(
                "AsyncAuthorizedSession"
            )

            class TripChannel:
                def __getattr__(self, name):
                    return trip("channel." + name)

            def expect(call):
                global count
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("Operations constructor escaped")

            inert_transport = operations_transport_base.OperationsTransport(
                credentials=anonymous
            )
            for call in (
                lambda: operations_rest.OperationsRestTransport(
                    credentials=anonymous
                ),
                lambda: operations_rest_asyncio.AsyncOperationsRestTransport(
                    credentials=anonymous
                ),
                lambda: operations_v1.OperationsClient(TripChannel()),
                lambda: operations_v1.OperationsAsyncClient(TripChannel()),
                lambda: operations_v1.AbstractOperationsClient(
                    transport=inert_transport
                ),
                lambda: operations_v1.AsyncOperationsRestClient(
                    transport=inert_transport
                ),
                lambda: operations_base_client.AbstractOperationsBaseClient(
                    transport=inert_transport
                ),
            ):
                expect(call)

            assert side_effects == [], side_effects
            print(f"operations-constructor-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "operations-constructor-canaries=7",
        )

    def test_gaos_constructor_aliases_fail_before_httpx_clients(self) -> None:
        code = textwrap.dedent(
            """
            from google.genai import _gaos
            from google.genai._gaos import google_genai, sdk
            from sanad_test_guard import (
                HermeticTestViolation, acknowledge_violation,
            )

            side_effects = []
            count = 0

            def trip(kind):
                def reached(*args, **kwargs):
                    side_effects.append((kind, args, kwargs))
                    raise AssertionError("GAOS HTTP client reached: " + kind)
                return reached

            sdk.httpx.Client = trip("httpx.Client")
            sdk.httpx.AsyncClient = trip("httpx.AsyncClient")

            def expect(call):
                global count
                try:
                    call()
                except HermeticTestViolation as exc:
                    acknowledge_violation(exc)
                    count += 1
                else:
                    raise AssertionError("GAOS constructor alias escaped")

            for constructor in (
                _gaos.GenAI,
                sdk.GenAI,
                google_genai.GenAI,
                _gaos.AsyncGenAI,
                sdk.AsyncGenAI,
                google_genai.AsyncGenAI,
            ):
                expect(constructor)

            assert side_effects == [], side_effects
            print(f"gaos-alias-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=True),
            "gaos-alias-canaries=6",
        )

    def test_cloud_credential_factories_fail_before_file_or_key_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "service-account.json"
            secret.write_text(
                '{"private_key":"ROUND5-PRIVATE-KEY-SECRET"}',
                encoding="utf-8",
            )
            code = textwrap.dedent(
                """
                import builtins
                import io
                import os
                from cryptography.hazmat.primitives import serialization
                from google.cloud import firestore, storage
                from google.oauth2 import service_account
                from sanad_test_guard import (
                    HermeticTestViolation, acknowledge_violation,
                )

                secret = os.environ["ROUND5_CREDENTIAL_FILE"]
                side_effects = []
                count = 0
                real_open = builtins.open
                real_io_open = io.open

                def watch_open(real):
                    def guarded(file, *args, **kwargs):
                        try:
                            path = os.fspath(file)
                        except TypeError:
                            path = None
                        if path == secret:
                            side_effects.append(("open", path))
                            raise AssertionError("credential JSON opened")
                        return real(file, *args, **kwargs)
                    return guarded

                def trip(kind):
                    def reached(*args, **kwargs):
                        side_effects.append((kind, args, kwargs))
                        raise AssertionError("credential work reached: " + kind)
                    return reached

                builtins.open = watch_open(real_open)
                io.open = watch_open(real_io_open)
                serialization.load_pem_private_key = trip("private-key parse")
                service_account.Credentials.from_service_account_info = trip(
                    "service-account credential construction"
                )

                def expect(call):
                    global count
                    try:
                        call()
                    except HermeticTestViolation as exc:
                        acknowledge_violation(exc)
                        count += 1
                    else:
                        raise AssertionError("Cloud credential factory escaped")

                for call in (
                    lambda: storage.Client.from_service_account_json(secret),
                    lambda: firestore.Client.from_service_account_json(secret),
                    lambda: storage.Client.from_service_account_info({}),
                    lambda: firestore.Client.from_service_account_info({}),
                ):
                    expect(call)

                assert side_effects == [], side_effects
                print(f"cloud-credential-factory-canaries={count}")
                """
            )
            self.assert_ok(
                _child(
                    "-c",
                    code,
                    explicit=True,
                    extra_env={"ROUND5_CREDENTIAL_FILE": str(secret)},
                ),
                "cloud-credential-factory-canaries=4",
            )

    def test_metadata_agent_mtls_appengine_and_signers_fail_before_side_effects(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "credential-material.pem"
            secret.write_text("ROUND4-PRIVATE-KEY-AND-CERT-SECRET", encoding="utf-8")
            code = textwrap.dedent(
                """
                import asyncio
                import builtins
                import io
                import os
                import subprocess
                from sanad_test_guard import (
                    HermeticTestViolation, acknowledge_violation,
                )
                from google.auth import _agent_identity_utils, app_engine, crypt
                from google.auth.compute_engine import _metadata, credentials as gce
                from google.auth.compute_engine import _mtls as gce_mtls
                from google.auth.crypt import _cryptography_rsa, es
                from google.auth.transport import _custom_tls_signer, _mtls_helper, mtls
                from google.auth.aio.transport import mtls as aio_mtls

                secret = os.environ["ROUND4_SECRET_FILE"]
                side_effects = []
                real_open = builtins.open
                real_io_open = io.open
                def watch_open(real):
                    def guarded(file, *args, **kwargs):
                        if os.fspath(file) == secret:
                            side_effects.append(("open", secret))
                            raise AssertionError("credential material opened")
                        return real(file, *args, **kwargs)
                    return guarded
                builtins.open = watch_open(real_open)
                io.open = watch_open(real_io_open)
                def trip(kind):
                    def reached(*args, **kwargs):
                        side_effects.append((kind, args, kwargs))
                        raise AssertionError("credential side effect reached: " + kind)
                    return reached
                subprocess.run = trip("subprocess.run")
                subprocess.Popen = trip("subprocess.Popen")
                subprocess.check_output = trip("subprocess.check_output")
                request = trip("metadata request")
                _cryptography_rsa.serialization.load_pem_private_key = trip("rsa parse")
                es.serialization.load_pem_private_key = trip("ec parse")

                count = 0
                def expect(call):
                    global count
                    try:
                        call()
                    except HermeticTestViolation as exc:
                        acknowledge_violation(exc)
                        count += 1
                    else:
                        raise AssertionError("credential acquisition was not blocked")

                expect(lambda: _metadata.get(request, "instance/service-accounts"))
                expect(lambda: _metadata.get_service_account_info(request))
                expect(lambda: _metadata.get_service_account_token(request))
                expect(lambda: _metadata.ping(request))
                compute = gce.Credentials()
                expect(lambda: compute.refresh(request))
                expect(lambda: compute.universe_domain)
                expect(lambda: gce.IDTokenCredentials(
                    request, "https://round4.invalid", use_metadata_identity_endpoint=True
                ))

                expect(_agent_identity_utils.get_and_parse_agent_identity_certificate)
                expect(lambda: _agent_identity_utils._parse_cert_path_from_config(secret))
                expect(lambda: _mtls_helper.get_client_ssl_credentials(
                    certificate_config_path=secret
                ))
                expect(lambda: _mtls_helper._read_cert_file(secret))
                expect(lambda: _mtls_helper._run_cert_provider_command(
                    ["/round4/cert-provider"]
                ))
                expect(lambda: _mtls_helper.decrypt_private_key(b"secret", b"secret"))
                expect(lambda: mtls.default_client_encrypted_cert_source(secret, secret))
                expect(lambda: asyncio.run(aio_mtls.get_client_ssl_credentials(secret)))
                expect(lambda: gce_mtls.MdsMtlsAdapter())
                custom = _custom_tls_signer.CustomTlsSigner(secret)
                expect(custom.load_libraries)

                assert app_engine.app_identity is None
                expect(app_engine.get_project_id)
                expect(app_engine.Credentials)
                expect(lambda: app_engine.Signer().sign(b"secret"))
                expect(lambda: crypt.RSASigner.from_string(b"secret-private-key"))
                expect(lambda: _cryptography_rsa.RSASigner.from_string(
                    b"secret-private-key"
                ))
                expect(lambda: crypt.ES256Signer.from_string(b"secret-private-key"))

                assert side_effects == [], side_effects
                print(f"platform-acquisition-canaries={count}")
                """
            )
            self.assert_ok(
                _child(
                    "-c",
                    code,
                    explicit=True,
                    extra_env={"ROUND4_SECRET_FILE": str(secret)},
                ),
                "platform-acquisition-canaries=23",
            )

    def test_google_auth_version_drift_fails_before_guard_mutation(self) -> None:
        code = textwrap.dedent(
            """
            import os
            import socket
            import sanad_test_guard as guard
            before_project = os.environ["GOOGLE_CLOUD_PROJECT"]
            before_socket = socket.getaddrinfo
            guard.importlib.metadata.version = lambda name: "2.58.0"
            try:
                guard.install("version drift canary")
            except guard.HermeticBootstrapError as exc:
                assert "audited for google-auth==2.57.0" in str(exc)
                assert "found 2.58.0" in str(exc)
            else:
                raise AssertionError("google-auth drift did not fail bootstrap")
            assert os.environ["GOOGLE_CLOUD_PROJECT"] == before_project
            assert socket.getaddrinfo is before_socket
            print("version-drift-canary=1")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=None),
            "version-drift-canary=1",
        )

    def test_cloud_and_grpc_version_drift_fails_before_guard_mutation(self) -> None:
        code = textwrap.dedent(
            """
            import os
            import socket
            import sanad_test_guard as guard

            real_version = guard.importlib.metadata.version
            before_environment = dict(os.environ)
            before_socket = socket.getaddrinfo
            cases = (
                ("grpcio", "1.84.0"),
                ("google-api-core", "2.35.0"),
                ("google-cloud-core", "2.8.0"),
                ("google-genai", "2.21.0"),
                ("google-cloud-firestore", "2.30.0"),
                ("google-cloud-tasks", "2.25.0"),
                ("google-cloud-storage", "3.14.0"),
            )
            count = 0
            for distribution, drifted in cases:
                guard.importlib.metadata.version = (
                    lambda name, distribution=distribution, drifted=drifted:
                    drifted if name == distribution else real_version(name)
                )
                try:
                    guard.install("dependency drift canary")
                except guard.HermeticBootstrapError as exc:
                    assert distribution in str(exc), (distribution, exc)
                    assert drifted in str(exc), (drifted, exc)
                    count += 1
                else:
                    raise AssertionError(distribution + " drift did not fail")
                assert dict(os.environ) == before_environment
                assert socket.getaddrinfo is before_socket
            print(f"dependency-drift-canaries={count}")
            """
        )
        self.assert_ok(
            _child("-c", code, explicit=None),
            "dependency-drift-canaries=7",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

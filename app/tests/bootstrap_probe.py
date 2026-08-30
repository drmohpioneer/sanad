"""Small subprocess target proving the guard ran before application imports."""

from __future__ import annotations

import importlib.metadata
import os
import unittest

from sanad_test_guard import (
    PINNED_API_CORE_VERSION,
    PINNED_CLOUD_CORE_VERSION,
    PINNED_FIRESTORE_VERSION,
    PINNED_GOOGLE_AUTH_VERSION,
    PINNED_GRPCIO_VERSION,
    PINNED_GENAI_VERSION,
    PINNED_STORAGE_VERSION,
    PINNED_TASKS_VERSION,
    TEST_CONFIG,
    TEST_CREDENTIALS,
    TEST_PROJECT,
    guards_installed,
    install_source,
    is_blocked_genai_client,
)


if not guards_installed():
    raise RuntimeError("the hermetic guard was not installed before the probe")

from core import media, store  # noqa: E402 - the assertion must precede this


class BootstrapProbe(unittest.TestCase):
    def test_the_process_was_guarded_before_core_imports(self) -> None:
        self.assertEqual(install_source(), "sitecustomize")
        self.assertEqual(os.environ["GOOGLE_CLOUD_PROJECT"], TEST_PROJECT)
        self.assertEqual(os.environ["CLOUDSDK_CONFIG"], TEST_CONFIG)
        self.assertEqual(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], TEST_CREDENTIALS
        )
        self.assertEqual(store.PROJECT, TEST_PROJECT)
        self.assertTrue(is_blocked_genai_client(media.client))
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
            with self.subTest(distribution=distribution):
                self.assertEqual(importlib.metadata.version(distribution), expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

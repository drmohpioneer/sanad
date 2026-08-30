"""Start Sanad's test boundary before unittest imports application modules."""

from sanad_test_process import is_test_process


if is_test_process():
    try:
        from sanad_test_guard import install

        install("sitecustomize")
    except Exception as exc:
        # ``site.execsitecustomize`` prints and suppresses ordinary exceptions.
        # Turn a broken hermetic boundary into a startup failure instead of
        # allowing a supported test command to continue without its guards.
        raise SystemExit(
            "Sanad hermetic unittest bootstrap failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
